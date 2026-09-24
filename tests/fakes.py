"""A fake Snowflake connection that simulates running, succeeded, failed and
cancelled statuses (spec 11)."""

from __future__ import annotations

import glob
import gzip
import hashlib
import mimetypes
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FakeProgrammingError(Exception):
    def __init__(
        self,
        msg: str,
        errno: int | None = None,
        sqlstate: str | None = None,
        sfqid: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.raw_msg = msg
        self.msg = msg
        self.errno = errno
        self.sqlstate = sqlstate
        self.sfqid = sfqid


@dataclass
class FakeStatement:
    """How the fake connection should behave for a statement."""

    columns: list[tuple] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: Exception | None = None
    #: Number of poll calls that report "still running" before finishing.
    polls: int = 0
    rowcount: int | None = None


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.sfqid: str | None = None
        self.description: list[tuple] | None = None
        self.rowcount: int = -1
        self._rows: list[tuple] = []
        self._pos = 0
        self.closed = False
        #: Set by get_results_from_sfqid and applied on the first fetch, the
        #: way the real connector's _prefetch_hook works: an async cursor has
        #: no description until rows are pulled from it.
        self._pending: FakeStatement | None = None

    # -- DB-API-ish --------------------------------------------------------

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        status = self.conn.status_rows(sql)
        if status is not None:
            self.description, rows = status
            self._rows = rows
            self._pos = 0
            return self
        if self.conn.stage is not None:
            answer = self.conn.stage.handle(sql)
            if answer is not None:
                self.conn.executed.append(sql)
                self.sfqid = self.conn.next_qid()
                self.description, self._rows = answer
                self._pos = 0
                return self
        self.conn.executed.append(sql)
        if sql.upper().startswith("SELECT SYSTEM$CANCEL_QUERY"):
            self.conn.cancel_requested.set()
            qid = params[0] if params else None
            self.conn.cancelled_ids.append(qid)
            self.description = [("SYSTEM$CANCEL_QUERY", 2, None, None, None, None, True)]
            self._rows = [("cancelled",)]
            return self
        spec = self.conn.plan_for(sql)
        if spec.error is not None:
            raise spec.error
        self.conn.track_transaction(sql)
        self._apply(spec)
        return self

    def execute_async(self, sql: str) -> FakeCursor:
        self.conn.executed.append(sql)
        if self.conn.plan_for(sql).error is None:
            self.conn.track_transaction(sql)
        self.sfqid = self.conn.next_qid()
        self.conn.pending[self.sfqid] = (sql, self.conn.plan_for(sql).polls)
        return self

    def get_results_from_sfqid(self, sfqid: str) -> None:
        """Arm the result without materialising it, as the connector does."""
        sql, _ = self.conn.pending.get(sfqid, ("", 0))
        self._pending = self.conn.plan_for(sql)
        self.sfqid = sfqid

    def _prefetch(self) -> None:
        if self._pending is not None:
            spec, self._pending = self._pending, None
            self._apply(spec)

    def _apply(self, spec: FakeStatement) -> None:
        self.description = spec.columns or None
        self._rows = list(spec.rows)
        self._pos = 0
        self.rowcount = spec.rowcount if spec.rowcount is not None else len(spec.rows)

    def fetchmany(self, size: int) -> list[tuple]:
        self._prefetch()
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchall(self) -> list[tuple]:
        self._prefetch()
        chunk = self._rows[self._pos :]
        self._pos = len(self._rows)
        return chunk

    def fetchone(self) -> tuple | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def close(self) -> None:
        self.closed = True


_DML = re.compile(r"^\s*(insert|update|delete|merge)\b", re.IGNORECASE)
_DDL = re.compile(r"^\s*(create|drop|alter|truncate)\b", re.IGNORECASE)
_SET_AUTOCOMMIT = re.compile(r"^\s*alter\s+session\s+set\s+autocommit\s*=\s*(\w+)", re.IGNORECASE)


class FakeConnection:
    """Implements the ``Connection`` protocol from :mod:`snowdesk.db.session`.

    Models just enough of Snowflake's transactions for the commit-mode
    indicator (Q10): AUTOCOMMIT, BEGIN / COMMIT / ROLLBACK, DML opening a
    transaction when AUTOCOMMIT is off, and DDL committing implicitly.
    """

    def __init__(self, plan: dict[str, FakeStatement] | None = None) -> None:
        self.plan = plan or {}
        self.default = FakeStatement()
        self.executed: list[str] = []
        self.pending: dict[str, tuple[str, int]] = {}
        self.cancelled_ids: list[str | None] = []
        self.cancel_requested = threading.Event()
        self.closed = False
        self.role = "ANALYST"
        self.warehouse = "COMPUTE_WH"
        self.database = "RAW"
        self.schema = "PUBLIC"
        #: SHOW WAREHOUSES rows, by name: the size the status bar shows.
        self.warehouses: dict[str, str] = {"COMPUTE_WH": "X-Small"}
        self._qid = 0
        self._polled: dict[str, int] = {}
        self.autocommit = True
        self.transaction_id: str | None = None
        self._txn = 0
        #: SnowDesk's own reads of the commit mode and open transaction.  Kept
        #: out of ``executed``, which is what the user ran.
        self.status_queries: list[str] = []
        #: Raised by those reads, to simulate them failing.
        self.status_error: Exception | None = None
        #: Stages and their files, when a test wants LIST, PUT, GET and REMOVE.
        self.stage: FakeStages | None = None

    # -- transactions ------------------------------------------------------

    def status_rows(self, sql: str) -> tuple[list[tuple], list[tuple]] | None:
        upper = " ".join(sql.upper().split())
        if upper == "SELECT CURRENT_TRANSACTION()":
            column = [("CURRENT_TRANSACTION()", 2, None, None, None, None, True)]
            rows = [(self.transaction_id,)]
        elif upper.startswith("SHOW PARAMETERS LIKE 'AUTOCOMMIT'"):
            column = [("key", 2, None, None, None, None, False)]
            rows = [("AUTOCOMMIT", str(self.autocommit).lower(), "true", "SESSION")]
        elif upper.startswith("SHOW WAREHOUSES LIKE "):
            column = [("name", 2, None, None, None, None, False)]
            column.append(("size", 2, None, None, None, None, True))
            pattern = sql.split("LIKE", 1)[1].strip().strip("'").upper()
            rows = [(n, size) for n, size in self.warehouses.items() if n.upper() == pattern]
        else:
            return None
        self.status_queries.append(sql)
        if self.status_error is not None:
            raise self.status_error
        return column, rows

    def track_transaction(self, sql: str) -> None:
        upper = " ".join(sql.upper().split())
        if upper.startswith(("BEGIN", "START TRANSACTION")):
            self._open()
        elif upper.startswith(("COMMIT", "ROLLBACK")):
            self.transaction_id = None
        elif match := _SET_AUTOCOMMIT.match(sql):
            self.autocommit = match.group(1).lower() == "true"
        elif _DDL.match(sql):
            self.transaction_id = None
        elif _DML.match(sql) and not self.autocommit:
            self._open()

    def _open(self) -> None:
        if self.transaction_id is None:
            self._txn += 1
            self.transaction_id = f"17000000000{self._txn:02d}"

    def next_qid(self) -> str:
        self._qid += 1
        return f"01b0-{self._qid:04d}"

    def plan_for(self, sql: str) -> FakeStatement:
        for key, spec in self.plan.items():
            if key.lower() in sql.lower():
                return spec
        return self.default

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    # -- async status ------------------------------------------------------

    def get_query_status_throw_if_error(self, sfqid: str) -> str:
        sql, polls = self.pending.get(sfqid, ("", 0))
        seen = self._polled.get(sfqid, 0)
        self._polled[sfqid] = seen + 1
        if self.cancel_requested.is_set():
            raise FakeProgrammingError("SQL execution canceled", errno=604, sfqid=sfqid)
        spec = self.plan_for(sql)
        if seen >= polls:
            if spec.error is not None:
                raise spec.error
            return "SUCCESS"
        return "RUNNING"

    def is_still_running(self, status: str) -> bool:
        return status == "RUNNING"


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


#: Where a stage's files really live; PATTERN is matched against
#: ``STORAGE/<path from the stage root>``, never against the LIST name.
STORAGE = "sfc-stages/a1b2c3/stages/9f8e7d"


class FakeOperationalError(Exception):
    """The connector's OperationalError, which PUT and GET also raise."""

    def __init__(self, msg: str, errno: int | None = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.errno = errno


class FakeResultStatus(Enum):
    """The connector reports PUT and GET statuses as its own enum, not text."""

    UPLOADED = "UPLOADED"
    DOWNLOADED = "DOWNLOADED"
    SKIPPED = "SKIPPED"


def _col(name: str, type_code: int = 2) -> tuple:
    return (name, type_code, None, None, None, None, True)


#: A run of bare characters and quoted literals, so `PATTERN='^(a b)$'` and
#: `'file:///a b/x.csv'` each stay one token.
_TOKEN = re.compile(r"(?:[^\s']|'(?:[^']|'')*')+")


def _unquote(token: str) -> str:
    """Undo ``quote_literal``: doubled quotes, then doubled backslashes."""
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'").replace("\\\\", "\\")
    return token


@dataclass
class FakeStages:
    """Just enough of Snowflake's stages to exercise SnowDesk's transfers.

    Files are held by their raw ``LIST`` name: a named stage prefixes its own
    lower-cased name, the user and table stages do not.  The shapes of the PUT
    and GET results follow ``file_transfer_agent.result()`` in connector 4.7.

    PATTERN behaves as measured against a real account: it must match the
    whole of an internal path, ``<storage prefix>/<path from the stage root>``,
    not the name LIST shows.  A GET that matches nothing raises, as the
    connector does.
    """

    #: SHOW STAGES rows: name, database_name, schema_name, type, url.
    stages: list[dict[str, str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    #: Raised by the n-th PUT/GET/REMOVE (1-based), to simulate failures.
    fail_on: dict[int, Exception] = field(default_factory=dict)
    #: Called before each transfer statement; lets a test press Stop mid-run.
    before_transfer: Any = None
    transfers: int = 0

    def handle(self, sql: str) -> tuple[list[tuple], list[tuple]] | None:
        tokens = _TOKEN.findall(sql)
        verb = tokens[0].upper() if tokens else ""
        if verb == "SHOW" and "STAGES" in sql.upper():
            columns = [_col(n) for n in ("name", "database_name", "schema_name", "type", "url")]
            rows = [
                (
                    s["name"],
                    s["database_name"],
                    s["schema_name"],
                    s.get("type", "INTERNAL"),
                    s.get("url", ""),
                )
                for s in self.stages
            ]
            return columns, rows
        pattern = None
        for token in tokens:
            if token.upper().startswith("PATTERN="):
                pattern = _unquote(token.split("=", 1)[1])
        if verb == "LIST":
            return self._list(self._prefix(tokens[1]), self._root(tokens[1]), pattern)
        if verb in ("PUT", "GET", "REMOVE"):
            self.transfers += 1
            if self.before_transfer is not None:
                self.before_transfer(self.transfers)
            error = self.fail_on.get(self.transfers)
            if error is not None:
                raise error
            options = dict(t.split("=", 1) for t in tokens if "=" in t and not t.startswith("'"))
            if verb == "PUT":
                return self._put(_unquote(tokens[1]), self._prefix(tokens[2]), options)
            if verb == "GET":
                return self._get(
                    self._prefix(tokens[1]), self._root(tokens[1]), _unquote(tokens[2]), pattern
                )
            return self._remove(self._prefix(tokens[1]), self._root(tokens[1]), pattern)
        return None

    # -- locations ---------------------------------------------------------

    def _prefix(self, token: str) -> str:
        """``@DB.S.LANDING/a/`` → ``landing/a/``; ``@~/a`` → ``a``."""
        loc = _unquote(token)
        assert loc.startswith("@"), loc
        stage, _sep, path = loc[1:].partition("/")
        if stage == "~" or stage.rpartition(".")[2].startswith("%"):
            return path
        name = stage.rpartition(".")[2].strip('"').lower()
        return f"{name}/{path}"

    def _root(self, token: str) -> str:
        """The part of a raw name that is the stage's own: ``landing/`` or nothing."""
        stage = _unquote(token)[1:].partition("/")[0]
        if stage == "~" or stage.rpartition(".")[2].startswith("%"):
            return ""
        return stage.rpartition(".")[2].strip('"').lower() + "/"

    # -- statements --------------------------------------------------------

    def _list(
        self, prefix: str, root: str = "", pattern: str | None = None
    ) -> tuple[list[tuple], list[tuple]]:
        columns = [_col("name"), _col("size", 0), _col("md5"), _col("last_modified")]
        rows = [
            (
                name,
                len(self.files[name]),
                hashlib.md5(self.files[name]).hexdigest(),
                "Wed, 24 Sep 2026 09:12:00 GMT",
            )
            for name in self._matching(prefix, root, pattern)
        ]
        return columns, rows

    def _put(
        self, source: str, prefix: str, options: dict[str, str]
    ) -> tuple[list[tuple], list[tuple]]:
        assert source.startswith("file://"), source
        matches = glob.glob(source[len("file://") :])
        assert len(matches) == 1, f"{source} matched {matches}"
        local = matches[0]
        data = Path(local).read_bytes()
        name = os.path.basename(local)
        _type, encoding = mimetypes.guess_type(name)
        compressed = options.get("AUTO_COMPRESS", "TRUE") == "TRUE" and encoding is None
        target = name + ".gz" if compressed else name
        stored = gzip.compress(data) if compressed else data
        key = prefix + target
        if key in self.files and options.get("OVERWRITE", "FALSE") != "TRUE":
            status = FakeResultStatus.SKIPPED
        else:
            self.files[key] = stored
            status = FakeResultStatus.UPLOADED
        columns = [
            _col("source"),
            _col("target"),
            _col("source_size", 0),
            _col("target_size", 0),
            _col("source_compression"),
            _col("target_compression"),
            _col("status"),
            _col("message"),
        ]
        row = (
            name,
            target,
            len(data),
            len(stored),
            "NONE",
            "GZIP" if compressed else "NONE",
            status,
            "",
        )
        return columns, [row]

    def _matching(self, prefix: str, root: str, pattern: str | None) -> list[str]:
        names = [n for n in sorted(self.files) if n.startswith(prefix)]
        if pattern is not None:
            names = [n for n in names if re.fullmatch(pattern, f"{STORAGE}/{n[len(root) :]}")]
        return names

    def _get(
        self, prefix: str, root: str, target: str, pattern: str | None
    ) -> tuple[list[tuple], list[tuple]]:
        assert target.startswith("file://") and target.endswith("/"), target
        directory = Path(target[len("file://") :])
        assert directory.is_dir(), f"GET target {directory} does not exist"
        matched = self._matching(prefix, root, pattern)
        if not matched:
            raise FakeOperationalError(
                "While getting file(s) there was an error: the file does not exist.", errno=253006
            )
        rows = []
        for name in matched:
            # Flattened to the bare name, as the real connector does.
            (directory / os.path.basename(name)).write_bytes(self.files[name])
            rows.append(
                (os.path.basename(name), len(self.files[name]), FakeResultStatus.DOWNLOADED, "")
            )
        return [_col("file"), _col("size", 0), _col("status"), _col("message")], rows

    def _remove(
        self, prefix: str, root: str, pattern: str | None
    ) -> tuple[list[tuple], list[tuple]]:
        removed = self._matching(prefix, root, pattern)
        for name in removed:
            del self.files[name]
        return [_col("name"), _col("result")], [(n, "removed") for n in removed]
