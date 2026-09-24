"""Stages: listing, and moving files in and out with PUT, GET and REMOVE.

Everything that turns a stage, a stage path or a local path into SQL lives
here and nowhere else (docs/stage-browser.md §6).  PUT and GET take no bind
parameters, so paths are embedded as literals; names that could not be
embedded safely are refused before anything runs rather than escaped and
hoped for.

Three things about the connector and the server shape what follows:

* Stage paths match by *prefix*.  ``REMOVE @s/data`` also removes
  ``data2.csv``, so folders are always addressed with a trailing ``/`` and
  single files by a ``PATTERN`` -- which Snowflake matches in a way that has
  to be checked before anything is removed (see :func:`exact_pattern`).
* GET writes every file under its bare name, whatever folder it came from,
  so ``a/x.csv`` and ``b/x.csv`` overwrite each other.  Downloads are issued
  one stage folder at a time, into a matching local folder.
* The connector cannot interrupt a transfer, and in 4.x never calls its
  progress callbacks, so progress and Stop both work one statement at a time.
"""

from __future__ import annotations

import glob
import logging
import mimetypes
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from snowdesk.db.errors import is_session_lost, to_query_error
from snowdesk.db.identifiers import qualify, quote_ident, quote_literal
from snowdesk.db.session import Connection
from snowdesk.model import (
    DownloadItem,
    FileResult,
    FileStatus,
    RunStatus,
    StageFile,
    StageKind,
    StageRef,
    Statement,
    StatementOutcome,
    TransferKind,
    TransferPlan,
    TransferProgress,
    TransferSummary,
    UploadItem,
)
from snowdesk.util.formatting import format_bytes

log = logging.getLogger(__name__)

#: Chunks of a single file uploaded at once; the connector's own default.
PUT_PARALLEL = 4
#: Files named in one GET or REMOVE ``PATTERN``.  Keeps the pattern, and the
#: statement recorded in History, a readable size.
PATTERN_BATCH = 50
#: Enough for any real drop target, and a stop for a stage with millions of
#: files that would otherwise be read in full to check for collisions.
PLAN_LIST_CAP = 200_000

#: What could not be embedded in a PUT, GET or LIST literal safely.  A quote
#: or backslash would have to survive both the SQL literal and the path the
#: server hands back to the connector, and nothing checks that end to end.
_UNSAFE = re.compile(r"['\\\x00-\x1f\x7f]")
#: A stage location that can be written without quoting.
_BARE_LOCATION = re.compile(r"^[A-Za-z0-9_$@~%./=+-]+$")
#: Regex metacharacters, each escaped as a one-character bracket expression.
#: Brackets rather than backslashes: ``[.]`` is what Snowflake's own examples
#: use and what was measured to work, and it keeps backslashes, which the SQL
#: literal would have to double, out of the pattern altogether.
_REGEX_SPECIAL = re.compile(r"([.\[\]{}()*+?$|])")


class UnsafeName(ValueError):
    """A path SnowDesk will not put into a PUT, GET, LIST or REMOVE."""


class TransferStopped(Exception):
    """Raised between statements once Stop has been pressed."""


# --------------------------------------------------------------------------
# Names and locations
# --------------------------------------------------------------------------


def check_name(text: str, what: str = "name") -> None:
    """Refuse a path that cannot be embedded in a PUT or GET literal."""
    if _UNSAFE.search(text):
        raise UnsafeName(
            f"The {what} {text!r} contains a quote, a backslash or a control "
            "character, which SnowDesk cannot pass to Snowflake safely. Rename it."
        )


def stage_name(stage: StageRef) -> str:
    """The stage itself, without a path: ``@DB.SCHEMA.S``, ``@~``, ``@DB.SCHEMA.%T``."""
    if stage.kind is StageKind.USER:
        return "@~"
    if stage.kind is StageKind.TABLE:
        prefix = qualify(stage.database, stage.schema)
        table = f"%{quote_ident(stage.name)}"
        return f"@{prefix}.{table}" if prefix else f"@{table}"
    return "@" + qualify(stage.database, stage.schema, stage.name)


def location(stage: StageRef, path: str = "") -> str:
    """``stage_name`` plus a stage-relative path, quoted when it must be."""
    check_name(path, "stage path")
    raw = stage_name(stage) + ("/" + path.lstrip("/") if path else "")
    if _BARE_LOCATION.match(raw):
        return raw
    # Spaces and quoted identifiers need the whole location in quotes.
    return quote_literal(raw)


def file_url(path: str | Path, *, directory: bool = False) -> str:
    """A quoted ``file://`` URL for a local file or directory.

    The connector expands a PUT source with ``glob``, so a file literally named
    ``data[1].csv`` would otherwise match nothing, or something else; the
    source is escaped for glob.  A GET target is a plain directory.
    """
    absolute = os.path.abspath(os.fspath(path))
    check_name(absolute, "local path")
    target = absolute.rstrip("/") + "/" if directory else glob.escape(absolute)
    return quote_literal("file://" + target)


def regex_escape(text: str) -> str:
    """Escape ``text`` for PATTERN.  ``^`` has no bracket form and is refused."""
    if "^" in text:
        raise UnsafeName(
            f"The stage path {text!r} contains '^', which SnowDesk cannot match exactly. Rename it."
        )
    return _REGEX_SPECIAL.sub(lambda m: "[]]" if m.group(1) == "]" else f"[{m.group(1)}]", text)


def exact_pattern(names: Iterable[str]) -> str:
    """A PATTERN for these stage-relative paths, e.g. ``'^.*/(p/a[.]csv)$'``.

    Measured against a real account (docs/stage-browser.md §13): PATTERN must
    match the whole of a string that is *not* the name ``LIST`` shows but a
    longer internal path ending in ``/<path from the stage root>``.  Hence the
    leading ``.*/``.  That also matches the same path further down --
    ``p/x/p/a.csv`` -- so a pattern is never trusted on its own: callers check
    it with ``LIST`` first (:func:`matching`).
    """
    unique = sorted(set(names))
    if not unique:
        raise ValueError("a pattern needs at least one name")
    for name in unique:
        check_name(name, "stage path")
    body = "|".join(regex_escape(n) for n in unique)
    return quote_literal(f"^.*/({body})$")


def folder_of(name: str) -> str:
    """``a/b/c.csv`` → ``a/b/``; a root file → ``""``; a folder → its parent."""
    trimmed = name.rstrip("/")
    head, sep, _tail = trimmed.rpartition("/")
    return head + sep


def basename(name: str) -> str:
    return name.rstrip("/").rpartition("/")[2]


def relative_name(stage: StageRef, raw: str) -> str:
    """Turn a ``LIST`` name into a path relative to the stage's root.

    A named internal stage lists as ``landing/2026-09/x.csv`` -- its own name,
    lower-cased, first.  An external stage lists full URLs.  The user stage
    and table stages list bare paths.
    """
    if not stage.internal:
        for url in _urls(stage.url):
            if raw.startswith(url):
                return raw[len(url) :].lstrip("/")
        _scheme, sep, rest = raw.partition("://")
        if sep:
            # s3://bucket/key -> key
            return rest.partition("/")[2]
        return raw
    if stage.kind is StageKind.NAMED:
        first, sep, rest = raw.partition("/")
        if sep and first.lower() == stage.name.lower():
            return rest
    return raw


def _urls(value: str) -> list[str]:
    """``SHOW STAGES`` may report one URL or a JSON-ish list of them."""
    cleaned = value.strip().strip("[]")
    return [u.strip().strip('"') for u in cleaned.split(",") if u.strip().strip('"')]


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


def show_stages_sql() -> str:
    return "SHOW STAGES IN ACCOUNT"


def list_sql(stage: StageRef, prefix: str = "") -> str:
    return f"LIST {location(stage, prefix)}"


def put_sql(local: str | Path, stage: StageRef, folder: str, *, overwrite: bool) -> str:
    return (
        f"PUT {file_url(local)} {location(stage, folder)} "
        f"AUTO_COMPRESS=TRUE OVERWRITE={'TRUE' if overwrite else 'FALSE'} "
        f"PARALLEL={PUT_PARALLEL}"
    )


def get_sql(stage: StageRef, where: str, local_dir: str | Path, names: Sequence[str]) -> str:
    """GET the files ``names`` (stage-relative) from under ``where``."""
    return (
        f"GET {location(stage, where)} {file_url(local_dir, directory=True)} "
        f"PATTERN={exact_pattern(names)}"
    )


def remove_sql(stage: StageRef, where: str, names: Sequence[str] | None = None) -> str:
    """Remove a whole folder (``names`` is None) or these files from under ``where``.

    A folder must end in ``/`` -- without it, the prefix would also take any
    sibling whose name merely starts the same way.
    """
    if names is None:
        if where and not where.endswith("/"):
            raise ValueError(f"folder {where!r} must end with '/'")
        return f"REMOVE {location(stage, where)}"
    return f"REMOVE {location(stage, where)} PATTERN={exact_pattern(names)}"


def matching_sql(stage: StageRef, where: str, names: Sequence[str]) -> str:
    """The LIST that shows what a GET or REMOVE with the same pattern would touch."""
    return f"LIST {location(stage, where)} PATTERN={exact_pattern(names)}"


def describe_sql(stage: StageRef) -> str:
    return f"DESCRIBE STAGE {qualify(stage.database, stage.schema, stage.name)}"


_FORMATS = {
    ".csv": "TYPE = CSV SKIP_HEADER = 1",
    ".tsv": "TYPE = CSV FIELD_DELIMITER = '\\t' SKIP_HEADER = 1",
    ".txt": "TYPE = CSV",
    ".json": "TYPE = JSON",
    ".ndjson": "TYPE = JSON",
    ".jsonl": "TYPE = JSON",
    ".parquet": "TYPE = PARQUET",
    ".avro": "TYPE = AVRO",
    ".orc": "TYPE = ORC",
    ".xml": "TYPE = XML",
}
_COMPRESSION_SUFFIXES = {".gz", ".gzip", ".bz2", ".zst", ".br", ".deflate", ".raw_deflate"}


def guess_format(name: str) -> str:
    """A ``FILE_FORMAT`` body from the file's extension, ignoring compression."""
    suffixes = [s.lower() for s in PurePosixPath(basename(name)).suffixes]
    while suffixes and suffixes[-1] in _COMPRESSION_SUFFIXES:
        suffixes.pop()
    return _FORMATS.get(suffixes[-1] if suffixes else "", "TYPE = CSV")


def copy_into_sql(stage: StageRef, name: str) -> str:
    """A ``COPY INTO`` for a file or folder, left for the user to finish.

    The target is a placeholder that does not parse, so running it unedited
    fails instead of loading into whatever table happens to be current.  A
    table stage already knows its table.
    """
    if stage.kind is StageKind.TABLE:
        target = qualify(stage.database, stage.schema, stage.name)
    else:
        target = "<table>"
    is_folder = name == "" or name.endswith("/")
    folder = name if is_folder else folder_of(name)
    lines = [f"COPY INTO {target}", f"FROM {location(stage, folder)}"]
    if not is_folder:
        lines.append(f"FILES = ({quote_literal(basename(name))})")
    lines.append(f"FILE_FORMAT = ({guess_format(name)})")
    return "\n".join(lines)


def select_file_sql(stage: StageRef, file: StageFile, limit: int = 100) -> str:
    """Query a staged file in place.

    Addressed by its own path, which as a prefix could also take a longer
    name beside it (``a.csv.bak``); METADATA$FILENAME shows if it did.  A
    query cannot be checked in advance the way a GET or REMOVE is, and its
    PATTERN is matched differently again, so none is used.  CSV needs no
    format to read; anything else needs a named file format, which SnowDesk
    cannot pick, so the placeholder says so.
    """
    fmt = guess_format(file.name)
    options = "" if fmt.startswith("TYPE = CSV") else " (FILE_FORMAT => '<file_format>')"
    return (
        "SELECT METADATA$FILENAME, METADATA$FILE_ROW_NUMBER, t.$1, t.$2, t.$3\n"
        f"FROM {location(stage, file.name)}{options} t\n"
        f"LIMIT {int(limit)}"
    )


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def _named_rows(cursor: Any, limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Rows as dicts, at most ``limit`` of them, and whether more remained."""
    description = getattr(cursor, "description", None)
    if not description:
        return [], False
    names = [str(c[0] if isinstance(c, tuple) else c.name).lower() for c in description]
    if limit is None:
        rows = cursor.fetchall()
        truncated = False
    else:
        rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
    return [dict(zip(names, row, strict=False)) for row in rows], truncated


def parse_stages(rows: Iterable[dict[str, Any]]) -> list[StageRef]:
    out: list[StageRef] = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        kind = str(r.get("type") or "INTERNAL").upper()
        out.append(
            StageRef(
                kind=StageKind.NAMED,
                name=str(name),
                database=str(r.get("database_name") or ""),
                schema=str(r.get("schema_name") or ""),
                internal=kind.startswith("INTERNAL"),
                url=str(r.get("url") or ""),
            )
        )
    out.sort(key=lambda s: (s.database.lower(), s.schema.lower(), s.name.lower()))
    return out


def list_stages(conn: Connection) -> list[StageRef]:
    """Every stage the role can see, in one query (ST1)."""
    cur = conn.cursor()
    try:
        cur.execute(show_stages_sql())
        rows, _ = _named_rows(cur)
        return parse_stages(rows)
    finally:
        cur.close()


def parse_files(stage: StageRef, rows: Iterable[dict[str, Any]]) -> list[StageFile]:
    out: list[StageFile] = []
    for r in rows:
        raw = r.get("name")
        if not raw:
            continue
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append(
            StageFile(
                name=relative_name(stage, str(raw)),
                raw=str(raw),
                size=size,
                md5=str(r.get("md5") or ""),
                last_modified=str(r.get("last_modified") or ""),
            )
        )
    return out


def list_files(
    conn: Connection, stage: StageRef, prefix: str = "", cap: int | None = None
) -> tuple[list[StageFile], bool]:
    """``LIST`` a stage or folder: its files, and whether ``cap`` cut it short (ST4)."""
    cur = conn.cursor()
    try:
        cur.execute(list_sql(stage, prefix))
        rows, truncated = _named_rows(cur, cap)
        files = parse_files(stage, rows)
        # LIST matches by prefix, so `a/` also brought back nothing else, but a
        # file prefix would; keep only what is really under it.
        if prefix:
            files = [f for f in files if f.name.startswith(prefix)]
        return files, truncated
    finally:
        cur.close()


# --------------------------------------------------------------------------
# A folder tree over a flat listing (ST3)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Folder:
    name: str
    #: Stage-relative path of the folder, ending in ``/``; ``""`` at the root.
    path: str
    folders: dict[str, Folder] = field(default_factory=dict)
    files: list[StageFile] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files) + sum(f.file_count for f in self.folders.values())

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files) + sum(f.size for f in self.folders.values())


def build_tree(files: Iterable[StageFile], root: str = "") -> Folder:
    """Group a ``LIST`` into folders by ``/``, starting at ``root``."""
    top = Folder(name=basename(root), path=root)
    for file in files:
        if not file.name.startswith(root):
            continue
        rest = file.name[len(root) :]
        parts = rest.split("/")
        node = top
        for part in parts[:-1]:
            if not part:
                continue
            child = node.folders.get(part)
            if child is None:
                child = Folder(name=part, path=f"{node.path}{part}/")
                node.folders[part] = child
            node = child
        if parts[-1]:
            node.files.append(file)
    _sort(top)
    return top


def _sort(folder: Folder) -> None:
    folder.files.sort(key=lambda f: f.name.lower())
    folder.folders = dict(sorted(folder.folders.items(), key=lambda kv: kv[0].lower()))
    for child in folder.folders.values():
        _sort(child)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def predicted_target(path: str | Path) -> str:
    """The name ``path`` will have on the stage once PUT has compressed it.

    Mirrors the connector's detection: an encoding ``mimetypes`` recognises,
    or the magic bytes of ORC, Parquet and zstd, mean the file goes up as it
    is; anything else is gzipped and gains ``.gz``.
    """
    name = os.path.basename(os.fspath(path))
    _type, encoding = mimetypes.guess_type(name)
    if encoding is not None or name.endswith(".br"):
        return name
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        head = b""
    if head[:3] == b"ORC" or head == b"PAR1" or head == b"\x28\xb5\x2f\xfd":
        return name
    return name + ".gz"


def _upload_sources(paths: Iterable[str | Path], folder: str) -> list[tuple[str, str]]:
    """``(local file, stage folder)`` for every file to upload.

    A dropped folder keeps its structure under ``folder``.  Hidden files are
    left out of a folder -- a ``.DS_Store`` in every directory is not what
    anyone meant to upload -- but a hidden file picked on its own goes up.
    """
    out: list[tuple[str, str]] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            base = path.parent
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                rel = Path(dirpath).relative_to(base).as_posix()
                for filename in sorted(filenames):
                    if filename.startswith("."):
                        continue
                    out.append((os.path.join(dirpath, filename), f"{folder}{rel}/"))
        else:
            out.append((str(path), folder))
    return out


def plan_upload(
    conn: Connection,
    transfer_id: str,
    stage: StageRef,
    folder: str,
    paths: Sequence[str | Path],
) -> TransferPlan:
    """Work out an upload into ``folder``, and what it would overwrite (ST9)."""
    plan = TransferPlan(transfer_id=transfer_id, kind=TransferKind.UPLOAD, stage=stage)
    if not stage.internal:
        raise UnsafeName("Files can only be uploaded to an internal stage.")
    check_name(folder, "stage folder")
    for local, target_folder in _upload_sources(paths, folder):
        try:
            check_name(local, "local path")
            check_name(target_folder, "stage folder")
            if not os.path.isfile(local) or not os.access(local, os.R_OK):
                raise UnsafeName(f"{local} is not a readable file.")
        except UnsafeName as exc:
            plan.refused.append((local, str(exc)))
            continue
        plan.uploads.append(
            UploadItem(
                local=local,
                folder=target_folder,
                target=predicted_target(local),
                size=os.path.getsize(local),
            )
        )
    if plan.uploads:
        existing, _ = list_files(conn, stage, folder, cap=PLAN_LIST_CAP)
        names = {f.name for f in existing}
        plan.conflicts = sorted({i.folder + i.target for i in plan.uploads} & names, key=str.lower)
    return plan


def _safe_local(root: Path, relative: str) -> Path:
    """``root / relative``, refusing anything that would land outside ``root``.

    Stage paths come from the server, and an object store will happily hold
    a key like ``../../.zshrc``.
    """
    parts = [p for p in relative.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise UnsafeName(f"{relative!r} is not a path SnowDesk will write to.")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise UnsafeName(f"{relative!r} would be written outside {root}.")
    return target


def plan_download(
    conn: Connection,
    transfer_id: str,
    stage: StageRef,
    selection: Sequence[str],
    local_root: str | Path,
) -> TransferPlan:
    """Work out a download of files and folders (ending ``/``, or ``""`` for the
    whole stage) into ``local_root``.

    Each selection is listed afresh: the tree may be stale, or capped, and a
    download should take what is on the stage now.  A folder keeps its own
    name and everything under it; a file lands directly in ``local_root``.
    """
    root = Path(local_root)
    plan = TransferPlan(
        transfer_id=transfer_id,
        kind=TransferKind.DOWNLOAD,
        stage=stage,
        local_root=str(root),
    )
    if not stage.internal:
        raise UnsafeName("Files can only be downloaded from an internal stage.")
    seen: set[str] = set()
    for chosen in selection:
        files, _ = list_files(conn, stage, chosen, cap=PLAN_LIST_CAP)
        if chosen and not chosen.endswith("/"):
            files = [f for f in files if f.name == chosen]
        base = folder_of(chosen)
        for file in files:
            if file.name in seen:
                continue
            seen.add(file.name)
            try:
                check_name(file.raw, "stage path")
                local = _safe_local(root, file.name[len(base) :])
            except UnsafeName as exc:
                plan.refused.append((file.name, str(exc)))
                continue
            plan.downloads.append(DownloadItem(file=file, local=str(local)))
    plan.conflicts = sorted(i.local for i in plan.downloads if os.path.exists(i.local))
    return plan


def plan_remove(transfer_id: str, stage: StageRef, selection: Sequence[StageFile]) -> TransferPlan:
    """A REMOVE of files, and of folders given as a ``StageFile`` whose name ends ``/``."""
    return TransferPlan(
        transfer_id=transfer_id,
        kind=TransferKind.REMOVE,
        stage=stage,
        removes=list(selection),
    )


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Callbacks:
    """How a running transfer reports back; each is called on its thread."""

    progress: Callable[[TransferProgress], None] = lambda _p: None
    file_done: Callable[[FileResult], None] = lambda _r: None
    statement: Callable[[StatementOutcome], None] = lambda _o: None


def _status(value: Any) -> str:
    """Result statuses arrive as the connector's own enum, or as text."""
    return str(getattr(value, "value", value) or "").upper()


class _Run:
    """One transfer's bookkeeping: counts, progress, History entries."""

    def __init__(
        self, conn: Connection, plan: TransferPlan, stop: threading.Event, cb: Callbacks
    ) -> None:
        self.conn = conn
        self.plan = plan
        self.stop = stop
        self.cb = cb
        self.counts: dict[FileStatus, int] = {}
        self.files_done = 0
        self.bytes_done = 0
        self.files_total = 0
        self.bytes_total = 0
        self.index = 0

    def report(self, name: str, status: FileStatus, detail: str = "", size: int = 0) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        self.files_done += 1
        self.bytes_done += size
        self.cb.file_done(FileResult(self.plan.transfer_id, name, status, detail))

    def progress(self, current: str) -> None:
        self.cb.progress(
            TransferProgress(
                transfer_id=self.plan.transfer_id,
                kind=self.plan.kind,
                files_done=self.files_done,
                files_total=self.files_total,
                bytes_done=self.bytes_done,
                bytes_total=self.bytes_total,
                current=current,
            )
        )

    def execute(self, sql: str) -> tuple[list[dict[str, Any]], StatementOutcome]:
        """Run one statement; always records it, then re-raises a failure."""
        if self.stop.is_set():
            raise TransferStopped
        statement = Statement(sql=sql, start=0, end=0, is_put_or_get=True)
        started = time.monotonic()
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            rows, _ = _named_rows(cur)
        except Exception as exc:
            error = to_query_error(exc, getattr(cur, "sfqid", None))
            outcome = StatementOutcome(
                index=self.index,
                statement=statement,
                status=RunStatus.ERROR,
                query_id=error.query_id,
                duration_s=time.monotonic() - started,
                message=error.formatted(),
                error=error,
            )
            self.index += 1
            self.cb.statement(outcome)
            raise
        finally:
            try:
                cur.close()
            except Exception:
                log.debug("Ignoring error closing a transfer cursor", exc_info=True)
        elapsed = time.monotonic() - started
        outcome = StatementOutcome(
            index=self.index,
            statement=statement,
            status=RunStatus.SUCCESS,
            query_id=getattr(cur, "sfqid", None),
            duration_s=elapsed,
            row_count=len(rows),
            message=f"{len(rows)} file{'' if len(rows) == 1 else 's'} ({elapsed:.2f}s)",
        )
        self.index += 1
        self.cb.statement(outcome)
        return rows, outcome


def run_transfer(
    conn: Connection, plan: TransferPlan, stop: threading.Event, cb: Callbacks | None = None
) -> TransferSummary:
    """Carry out ``plan`` one statement at a time, checking ``stop`` between them."""
    run = _Run(conn, plan, stop, cb or Callbacks())
    for name, reason in plan.refused:
        run.report(name, FileStatus.FAILED, reason)
    error = ""
    stopped = False
    try:
        if plan.kind is TransferKind.UPLOAD:
            _run_upload(run)
        elif plan.kind is TransferKind.DOWNLOAD:
            _run_download(run)
        else:
            _run_remove(run)
    except TransferStopped:
        stopped = True
    except _SessionLost as exc:
        error = str(exc)
    return TransferSummary(
        transfer_id=plan.transfer_id,
        kind=plan.kind,
        stage=plan.stage,
        counts=dict(run.counts),
        stopped=stopped,
        error=error,
    )


class _SessionLost(Exception):
    """The connection went away mid-transfer; nothing more can run."""


def _fail(run: _Run, exc: Exception, names: Sequence[tuple[str, int]]) -> None:
    """Mark ``names`` failed, and give up on the rest if the session is gone."""
    message = to_query_error(exc).message
    for name, size in names:
        run.report(name, FileStatus.FAILED, message, size)
    if is_session_lost(exc):
        raise _SessionLost(message) from exc


def _finish_unstarted(run: _Run, names: Iterable[tuple[str, int]]) -> None:
    for name, _size in names:
        run.report(name, FileStatus.NOT_STARTED)


def _run_upload(run: _Run) -> None:
    plan = run.plan
    conflicts = set(plan.conflicts)
    items = list(plan.uploads)
    run.files_total = len(items) + len(plan.refused)
    run.bytes_total = sum(i.size for i in items)
    for position, item in enumerate(items):
        target = item.folder + item.target
        if target in conflicts and not plan.replace:
            run.report(target, FileStatus.SKIPPED, "Already on the stage.", item.size)
            continue
        if run.stop.is_set():
            _finish_unstarted(run, ((i.folder + i.target, 0) for i in items[position:]))
            raise TransferStopped
        run.progress(os.path.basename(item.local))
        if not os.path.isfile(item.local):
            run.report(target, FileStatus.FAILED, f"{item.local} no longer exists.", item.size)
            continue
        try:
            sql = put_sql(item.local, plan.stage, item.folder, overwrite=target in conflicts)
            rows, _ = run.execute(sql)
        except UnsafeName as exc:
            run.report(target, FileStatus.FAILED, str(exc), item.size)
            continue
        except TransferStopped:
            _finish_unstarted(run, ((i.folder + i.target, 0) for i in items[position:]))
            raise
        except Exception as exc:
            try:
                _fail(run, exc, [(target, item.size)])
            except _SessionLost:
                _finish_unstarted(run, ((i.folder + i.target, 0) for i in items[position + 1 :]))
                raise
            continue
        _report_put(run, item, rows)
    run.progress("")


def _report_put(run: _Run, item: UploadItem, rows: list[dict[str, Any]]) -> None:
    """One PUT, one file: say what Snowflake did with it."""
    if not rows:
        run.report(item.folder + item.target, FileStatus.FAILED, "PUT returned no result.")
        return
    row = rows[0]
    target = str(row.get("target") or item.target)
    status = _status(row.get("status"))
    name = item.folder + target
    if target != item.target:
        # The collision check compared against the predicted name; say so if
        # the prediction was wrong, since that check may have missed one.
        log.info("Predicted %s for %s, but PUT wrote %s", item.target, item.local, target)
    if status == "UPLOADED":
        detail = _sizes(row)
        run.report(name, FileStatus.UPLOADED, detail, item.size)
    elif status == "SKIPPED":
        run.report(name, FileStatus.SKIPPED, "Already on the stage.", item.size)
    else:
        run.report(name, FileStatus.FAILED, str(row.get("message") or status), item.size)


def _sizes(row: dict[str, Any]) -> str:
    try:
        source = int(row.get("source_size") or 0)
        target = int(row.get("target_size") or 0)
    except (TypeError, ValueError):
        return ""
    if not source:
        return ""
    if source == target:
        return format_bytes(source)
    return f"{format_bytes(source)} → {format_bytes(target)}"


def _batches(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _lookup(run: _Run, where: str, names: Sequence[str], *, stoppable: bool = True) -> set[str]:
    """What a GET or REMOVE with this location and pattern would touch.

    A LIST with the same location and PATTERN, which the probe in
    docs/stage-browser.md §13 showed matching exactly as GET and REMOVE do.
    SnowDesk's own check, so it is not recorded in History.  Not stoppable
    once the statement it checks has run: that result has to be reported.
    """
    if stoppable and run.stop.is_set():
        raise TransferStopped
    cur = run.conn.cursor()
    try:
        cur.execute(matching_sql(run.plan.stage, where, names))
        rows, _ = _named_rows(cur)
    finally:
        try:
            cur.close()
        except Exception:
            log.debug("Ignoring error closing a lookup cursor", exc_info=True)
    stage = run.plan.stage
    return {relative_name(stage, str(r["name"])) for r in rows if r.get("name")}


def _resolve(
    run: _Run, folder: str, names: Sequence[str]
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str]]]:
    """Where to address ``names`` so that each PATTERN touches exactly them.

    Returns ``(location, names)`` steps, and the names refused with a reason.
    The whole batch from its folder when that is exact; otherwise each file
    from its own path, whose prefix rules out ``p/x/p/a.csv`` matching
    ``p/a.csv``.  Anything still not exact is left alone.
    """
    wanted = set(names)
    found = _lookup(run, folder, names)
    missing = [(n, "No longer on the stage.") for n in names if n not in found]
    if found <= wanted:
        present = [n for n in names if n in found]
        return ([(folder, present)] if present else []), missing
    steps: list[tuple[str, list[str]]] = []
    refused = list(missing)
    for name in names:
        if name not in found:
            continue
        alone = _lookup(run, name, [name])
        if alone == {name}:
            steps.append((name, [name]))
        else:
            others = ", ".join(sorted(alone - {name})[:3])
            refused.append(
                (name, f"Snowflake would also match {others}, so SnowDesk left it alone.")
            )
    return steps, refused


def _run_download(run: _Run) -> None:
    plan = run.plan
    conflicts = set(plan.conflicts)
    items = list(plan.downloads)
    run.files_total = len(items) + len(plan.refused)
    run.bytes_total = sum(i.file.size for i in items)

    wanted: list[DownloadItem] = []
    for item in items:
        if item.local in conflicts and not plan.replace:
            run.report(
                item.file.name, FileStatus.SKIPPED, "Already exists locally.", item.file.size
            )
        else:
            wanted.append(item)

    # One GET per stage folder, since GET flattens whatever it fetches.
    by_folder: dict[str, list[DownloadItem]] = {}
    for item in wanted:
        by_folder.setdefault(folder_of(item.file.name), []).append(item)
    groups = [
        (folder, batch)
        for folder, members in by_folder.items()
        for batch in _batches(members, PATTERN_BATCH)
    ]
    for position, (folder, batch) in enumerate(groups):
        if run.stop.is_set():
            _finish_unstarted(run, _names(groups[position:]))
            raise TransferStopped
        local_dir = Path(batch[0].local).parent
        by_name = {i.file.name: i for i in batch}
        run.progress(folder or basename(batch[0].file.name))
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            steps, refused = _resolve(run, folder, list(by_name))
            for name, reason in refused:
                run.report(name, FileStatus.FAILED, reason, by_name[name].file.size)
            for where, names in steps:
                rows, _ = run.execute(get_sql(plan.stage, where, local_dir, names))
                _report_get(run, [by_name[n] for n in names], rows)
                for n in names:
                    by_name.pop(n)
        except TransferStopped:
            _finish_unstarted(run, _names([(folder, list(by_name.values()))]))
            _finish_unstarted(run, _names(groups[position + 1 :]))
            raise
        except Exception as exc:
            try:
                _fail(run, exc, [(i.file.name, i.file.size) for i in by_name.values()])
            except _SessionLost:
                _finish_unstarted(run, _names(groups[position + 1 :]))
                raise
            continue
    run.progress("")


def _names(groups: Sequence[tuple[str, Sequence[DownloadItem]]]) -> list[tuple[str, int]]:
    return [(i.file.name, 0) for _folder, batch in groups for i in batch]


def _report_get(run: _Run, batch: Sequence[DownloadItem], rows: list[dict[str, Any]]) -> None:
    by_name = {basename(str(r.get("file") or "")): r for r in rows}
    for item in batch:
        row = by_name.get(basename(item.file.name))
        if row is None:
            run.report(item.file.name, FileStatus.FAILED, "Not downloaded.", item.file.size)
        elif _status(row.get("status")) == "DOWNLOADED":
            run.report(item.file.name, FileStatus.DOWNLOADED, item.local, item.file.size)
        else:
            message = str(row.get("message") or _status(row.get("status")))
            run.report(item.file.name, FileStatus.FAILED, message, item.file.size)


def _run_remove(run: _Run) -> None:
    plan = run.plan
    folders = [f for f in plan.removes if f.name.endswith("/") or not f.name]
    files = [f for f in plan.removes if f.name and not f.name.endswith("/")]
    # A file inside a folder that is going anyway needs no statement of its own.
    files = [f for f in files if not any(f.name.startswith(d.name) for d in folders)]
    by_folder: dict[str, list[StageFile]] = {}
    for file in files:
        by_folder.setdefault(folder_of(file.name), []).append(file)
    steps: list[tuple[str, Sequence[StageFile] | None]] = [(d.name, None) for d in folders]
    steps += [
        (folder, batch)
        for folder, members in by_folder.items()
        for batch in _batches(members, PATTERN_BATCH)
    ]
    run.files_total = len(folders) + len(files)
    for position, (folder, batch) in enumerate(steps):
        pending = [folder or "/"] if batch is None else [f.name for f in batch]
        if run.stop.is_set():
            _finish_unstarted(run, ((n, 0) for n in _remove_names(steps[position:])))
            raise TransferStopped
        run.progress(folder if batch is None else ", ".join(basename(n) for n in pending))
        try:
            if batch is None:
                # A folder by its trailing slash: plain prefix, no pattern.
                rows, _ = run.execute(remove_sql(plan.stage, folder))
                _report_remove(run, folder or "/", rows)
                pending = []
                continue
            resolved, refused = _resolve(run, folder, pending)
            for name, reason in refused:
                run.report(name, FileStatus.FAILED, reason)
                pending.remove(name)
            for where, names in resolved:
                run.execute(remove_sql(plan.stage, where, names))
                # Confirmed by looking again rather than from REMOVE's rows,
                # whose naming has not been measured.
                left = _lookup(run, where, names, stoppable=False)
                for name in names:
                    if name not in left:
                        run.report(name, FileStatus.REMOVED)
                    else:
                        run.report(name, FileStatus.FAILED, "Still on the stage after REMOVE.")
                    pending.remove(name)
        except TransferStopped:
            _finish_unstarted(run, ((n, 0) for n in pending))
            _finish_unstarted(run, ((n, 0) for n in _remove_names(steps[position + 1 :])))
            raise
        except Exception as exc:
            try:
                _fail(run, exc, [(n, 0) for n in pending])
            except _SessionLost:
                _finish_unstarted(run, ((n, 0) for n in _remove_names(steps[position + 1 :])))
                raise
            continue
    run.progress("")


def _remove_names(steps: Sequence[tuple[str, Sequence[StageFile] | None]]) -> list[str]:
    return [
        name
        for folder, batch in steps
        for name in ([folder or "/"] if batch is None else [f.name for f in batch])
    ]


def _report_remove(run: _Run, label: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        run.report(label, FileStatus.SKIPPED, "Nothing matched on the stage.")
        return
    noun = "file" if len(rows) == 1 else "files"
    run.report(label, FileStatus.REMOVED, f"{len(rows)} {noun}")
