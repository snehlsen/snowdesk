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

    def __str__(self) -> str:
        left = " · ".join(x for x in (self.role, self.warehouse) if x)
        scope = ".".join(x for x in (self.database, self.schema) if x)
        return " · ".join(x for x in (left, scope) if x) or "no context"


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
