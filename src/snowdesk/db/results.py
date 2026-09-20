"""Live result cursors and incremental fetching (R2, R4).

The worker keeps live cursors in a registry keyed by result ID and closes them
when a result tab is closed or a new run replaces it (spec 7.5).  Cursor
objects never cross the thread boundary — only lists of tuples do.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from snowdesk.model import ColumnInfo, columns_from_description

log = logging.getLogger(__name__)

_ids = itertools.count(1)


def new_result_id() -> str:
    return f"r{next(_ids)}"


@dataclass(slots=True)
class ResultHandle:
    """A live cursor plus the bookkeeping the grid needs.

    Column metadata is read on the first fetch, not at registration.  A cursor
    from ``execute_async`` carries no description until its prefetch hook runs,
    and that hook only runs when rows are first pulled; reading ``description``
    any earlier reports "no result set" for every asynchronous query.
    """

    result_id: str
    cursor: Any
    #: The statement's query id, kept so the full result can be re-read with
    #: RESULT_SCAN without disturbing the grid's own cursor (R6).
    query_id: str | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    loaded: int = 0
    exhausted: bool = False
    total: int | None = None
    closed: bool = False
    primed: bool = False

    def fetch(self, page_size: int) -> list[tuple[Any, ...]]:
        """Fetch the next page, marking the handle exhausted at the end."""
        if self.exhausted or self.closed:
            return []
        rows = self.cursor.fetchmany(page_size)
        if not self.primed:
            self._read_metadata()
        rows = [tuple(r) for r in rows] if rows else []
        self.loaded += len(rows)
        if len(rows) < page_size:
            self.exhausted = True
        return rows

    def _read_metadata(self) -> None:
        self.primed = True
        self.columns = columns_from_description(getattr(self.cursor, "description", None))
        self.total = _row_total(self.cursor)

    def iter_batches(self, page_size: int) -> Iterator[list[tuple[Any, ...]]]:
        """Yield remaining rows batch by batch, for streaming export (R6)."""
        while not self.exhausted and not self.closed:
            batch = self.fetch(page_size)
            if not batch:
                return
            yield batch

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.cursor.close()
        except Exception:
            log.debug("Ignoring error closing cursor %s", self.result_id, exc_info=True)


class ResultRegistry:
    """Owns every live result cursor for the session."""

    def __init__(self) -> None:
        self._handles: dict[str, ResultHandle] = {}

    def __contains__(self, result_id: object) -> bool:
        return result_id in self._handles

    def __len__(self) -> int:
        return len(self._handles)

    def register(self, cursor: Any) -> ResultHandle:
        """Take ownership of a cursor. Metadata is read on its first fetch."""
        handle = ResultHandle(
            result_id=new_result_id(),
            cursor=cursor,
            query_id=str(getattr(cursor, "sfqid", "") or "") or None,
        )
        self._handles[handle.result_id] = handle
        return handle

    def get(self, result_id: str) -> ResultHandle | None:
        return self._handles.get(result_id)

    def close(self, result_id: str) -> None:
        handle = self._handles.pop(result_id, None)
        if handle is not None:
            handle.close()

    def close_all(self) -> None:
        for handle in list(self._handles.values()):
            handle.close()
        self._handles.clear()


def _row_total(cursor: Any) -> int | None:
    """``cursor.rowcount`` when the connector reports one (spec 8)."""
    value = getattr(cursor, "rowcount", None)
    return value if isinstance(value, int) and value >= 0 else None


# --------------------------------------------------------------------------
# Telling a grid apart from a status line (Q6)
# --------------------------------------------------------------------------

#: Statements that always produce a grid, whatever their result looks like.
_QUERY_LEADERS = frozenset(
    {
        "SELECT",
        "WITH",
        "SHOW",
        "DESC",
        "DESCRIBE",
        "LIST",
        "LS",
        "CALL",
        "EXPLAIN",
        "TABLE",
        "VALUES",
    }
)

_COMMENT_STARTS = ("--", "//")


def leading_keyword(sql: str) -> str:
    """The first real keyword of ``sql``, skipping comments and brackets."""
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace() or ch == "(":
            i += 1
            continue
        if sql.startswith(_COMMENT_STARTS, i):
            end = sql.find("\n", i)
            if end == -1:
                return ""
            i = end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return ""
            i = end + 2
            continue
        break
    end = i
    while end < n and (sql[end].isalnum() or sql[end] == "_"):
        end += 1
    return sql[i:end].upper()


def summarize_status(
    columns: Sequence[ColumnInfo], rows: Sequence[Sequence[Any]], sql: str
) -> str | None:
    """A status line for a DML/DDL statement, or ``None`` when it needs a grid.

    Every statement run through ``execute_async`` comes back as a result set,
    because the connector reads it with ``RESULT_SCAN``.  Snowflake gives DDL
    and session commands a single ``status`` column, and DML a row of
    ``number of rows ...`` counters, so those shapes become a message instead
    of a result tab.

    The leading keyword is checked first so a genuine query is never mistaken
    for a status: ``SELECT status FROM t`` returning one row has exactly the
    shape of a DDL result.
    """
    if leading_keyword(sql) in _QUERY_LEADERS:
        return None
    if not columns:
        return "Statement executed."
    if len(rows) != 1:
        return None

    names = [c.name.strip().lower() for c in columns]
    row = rows[0]

    if len(columns) == 1 and names[0] == "status":
        return str(row[0])

    if all(name.startswith("number of rows") for name in names):
        parts = [
            f"{_as_int(value):,} {name.removeprefix('number of ')}"
            for name, value in zip(names, row, strict=False)
        ]
        return ", ".join(parts).capitalize()

    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
