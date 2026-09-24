"""Shared, dependency-free data types.

Everything here is plain Python: these are the objects that cross the worker →
UI boundary as Qt signal payloads, so they must never hold live cursors or
connector objects (spec 6.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Snowflake ``type_code`` → type family name.  The connector exposes the same
#: mapping as ``snowflake.connector.constants.FIELD_ID_TO_NAME``; it is repeated
#: here so formatting stays unit-testable without importing the connector.
FIELD_ID_TO_NAME: dict[int, str] = {
    0: "FIXED",
    1: "REAL",
    2: "TEXT",
    3: "DATE",
    4: "TIMESTAMP",
    5: "VARIANT",
    6: "TIMESTAMP_LTZ",
    7: "TIMESTAMP_TZ",
    8: "TIMESTAMP_NTZ",
    9: "OBJECT",
    10: "ARRAY",
    11: "BINARY",
    12: "TIME",
    13: "BOOLEAN",
    14: "GEOGRAPHY",
    15: "GEOMETRY",
    16: "VECTOR",
}

JSON_TYPES = frozenset({"VARIANT", "OBJECT", "ARRAY", "MAP"})
NUMERIC_TYPES = frozenset({"FIXED", "REAL"})
TIMESTAMP_TYPES = frozenset({"TIMESTAMP", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP_NTZ"})


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """One result column, as shown in the grid header (R1)."""

    name: str
    type_name: str
    precision: int | None = None
    scale: int | None = None
    nullable: bool = True

    @property
    def type_hint(self) -> str:
        """The second header line, e.g. ``NUMBER(38,2)``."""
        if self.type_name == "FIXED":
            base = "NUMBER"
            if self.precision is not None:
                return f"{base}({self.precision},{self.scale or 0})"
            return base
        if self.type_name == "REAL":
            return "FLOAT"
        if self.type_name == "TEXT":
            return "VARCHAR"
        return self.type_name

    @property
    def is_numeric(self) -> bool:
        return self.type_name in NUMERIC_TYPES

    @property
    def is_json(self) -> bool:
        return self.type_name in JSON_TYPES


def columns_from_description(description: Any) -> list[ColumnInfo]:
    """Build :class:`ColumnInfo` list from a DB-API ``cursor.description``.

    Accepts both the connector's ``ResultMetadata`` objects and plain 7-tuples.
    """
    out: list[ColumnInfo] = []
    for col in description or ():
        if isinstance(col, tuple):
            name, type_code, _display, _internal, precision, scale, nullable = col[:7]
        else:
            name = col.name
            type_code = col.type_code
            precision = getattr(col, "precision", None)
            scale = getattr(col, "scale", None)
            nullable = getattr(col, "is_nullable", True)
        type_name = (
            type_code
            if isinstance(type_code, str)
            else FIELD_ID_TO_NAME.get(int(type_code), f"TYPE_{type_code}")
        )
        out.append(
            ColumnInfo(
                name=str(name),
                type_name=type_name,
                precision=precision,
                scale=scale,
                nullable=bool(nullable),
            )
        )
    return out


class RunStatus(StrEnum):
    """Terminal status of one statement."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Current session context for the status bar (spec 7.6)."""

    role: str | None = None
    warehouse: str | None = None
    database: str | None = None
    schema: str | None = None
    #: As ``SHOW WAREHOUSES`` gives it ("X-Small", "Medium", ...), or ``None``
    #: when there is no warehouse or the role cannot see it.
    warehouse_size: str | None = None

    def __str__(self) -> str:
        warehouse = self.warehouse
        if warehouse and self.warehouse_size:
            warehouse = f"{warehouse} ({self.warehouse_size})"
        left = " · ".join(x for x in (self.role, warehouse) if x)
        scope = ".".join(x for x in (self.database, self.schema) if x)
        return " · ".join(x for x in (left, scope) if x) or "no context"


@dataclass(frozen=True, slots=True)
class TransactionState:
    """Commit mode and any open transaction, for the status bar (Q10).

    Read back from the session rather than tracked from what SnowDesk sent:
    a script can ``ALTER SESSION SET AUTOCOMMIT`` or ``BEGIN`` on its own, and
    DDL commits implicitly.
    """

    #: ``None`` while disconnected, or when the session could not be asked.
    autocommit: bool | None = None
    #: What ``CURRENT_TRANSACTION()`` returned; ``None`` when none is open.
    transaction_id: str | None = None

    @property
    def in_transaction(self) -> bool:
        return self.transaction_id is not None


@dataclass(frozen=True, slots=True)
class QueryError:
    """A Snowflake error, with everything needed for the Messages tab (Q5)."""

    message: str
    errno: int | None = None
    sqlstate: str | None = None
    query_id: str | None = None

    def formatted(self) -> str:
        parts: list[str] = []
        if self.errno is not None:
            parts.append(f"[{self.errno}]")
        if self.sqlstate:
            parts.append(f"(SQLSTATE {self.sqlstate})")
        head = " ".join(parts)
        body = f"{head} {self.message}".strip()
        if self.query_id:
            body = f"{body}\nQuery ID: {self.query_id}"
        return body


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement carved out of the editor text (Q2)."""

    sql: str
    #: Character offset of the statement in the original editor text, so the
    #: failing statement can be highlighted (Q5).
    start: int
    end: int
    is_put_or_get: bool = False


@dataclass(slots=True)
class StatementOutcome:
    """Everything the UI needs about one finished statement."""

    index: int
    statement: Statement
    status: RunStatus
    query_id: str | None = None
    duration_s: float = 0.0
    row_count: int | None = None
    message: str = ""
    error: QueryError | None = None
    result_id: str | None = None
    columns: list[ColumnInfo] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ObjectNode:
    """One node in the object browser tree (B1)."""

    name: str
    kind: str  # "database" | "schema" | "table" | "view" | "column"
    detail: str = ""
    path: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Stages (docs/stage-browser.md)
# --------------------------------------------------------------------------


class StageKind(StrEnum):
    NAMED = "named"
    USER = "user"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class StageRef:
    """A stage SnowDesk can list, and -- when internal -- transfer to and from.

    ``name`` is the stage's own name for a named stage and the table's name for
    a table stage; the user stage has neither database nor schema.
    """

    kind: StageKind
    name: str = ""
    database: str = ""
    schema: str = ""
    #: ``PUT`` and ``GET`` only work on internal stages (ST10).
    internal: bool = True
    #: Where an external stage points, as ``SHOW STAGES`` reports it.
    url: str = ""


@dataclass(frozen=True, slots=True)
class StageFile:
    """One ``LIST`` row."""

    #: The path relative to the stage's root, e.g. ``2026-09/orders.csv.gz``.
    name: str
    #: The name exactly as ``LIST`` returned it, which is what ``PATTERN``
    #: is matched against.
    raw: str
    size: int = 0
    md5: str = ""
    last_modified: str = ""


class TransferKind(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    REMOVE = "remove"


class FileStatus(StrEnum):
    """What happened to one file in a transfer."""

    UPLOADED = "uploaded"
    DOWNLOADED = "downloaded"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_STARTED = "not started"


@dataclass(frozen=True, slots=True)
class UploadItem:
    local: str
    #: Stage folder the file goes into: ``""`` for the root, else ``"a/b/"``.
    folder: str
    #: The name it is expected to get on the stage, once compressed.
    target: str


@dataclass(frozen=True, slots=True)
class DownloadItem:
    file: StageFile
    #: The full local path it will be written to.
    local: str


@dataclass(slots=True)
class TransferPlan:
    """What a transfer will do, worked out before anything moves.

    Built off the UI thread, since it needs a ``LIST``; the UI then asks about
    ``conflicts`` and hands the plan back to be run.
    """

    transfer_id: str
    kind: TransferKind
    stage: StageRef
    uploads: list[UploadItem] = field(default_factory=list)
    downloads: list[DownloadItem] = field(default_factory=list)
    #: Files to ``REMOVE``; a folder is one whose ``name`` ends ``/``.
    removes: list[StageFile] = field(default_factory=list)
    #: Where a download is written.
    local_root: str = ""
    #: Targets that already exist: stage names for an upload, local paths
    #: for a download.
    conflicts: list[str] = field(default_factory=list)
    #: Whether to replace ``conflicts`` (True) or leave them alone (False).
    replace: bool = False
    #: Files refused before anything ran, and why.
    refused: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FileResult:
    transfer_id: str
    name: str
    status: FileStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """What a running transfer is working on now; it carries no counts."""

    transfer_id: str
    kind: TransferKind
    #: The file (a folder, or a batch of names, for a download or delete).
    current: str


@dataclass(frozen=True, slots=True)
class TransferSummary:
    transfer_id: str
    kind: TransferKind
    stage: StageRef
    counts: dict[FileStatus, int] = field(default_factory=dict)
    #: Stopped by the user between files.
    stopped: bool = False
    #: Set when the transfer could not go on at all, e.g. the session died.
    error: str = ""
