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
    """A live cursor plus the bookkeeping the grid needs."""

    result_id: str
    cursor: Any
    columns: list[ColumnInfo]
    loaded: int = 0
    exhausted: bool = False
    total: int | None = None
    closed: bool = False
    _extra: dict[str, Any] = field(default_factory=dict)

    def fetch(self, page_size: int) -> list[tuple[Any, ...]]:
        """Fetch the next page, marking the handle exhausted at the end."""
        if self.exhausted or self.closed:
            return []
        rows = self.cursor.fetchmany(page_size)
        rows = [tuple(r) for r in rows] if rows else []
        self.loaded += len(rows)
        if len(rows) < page_size:
            self.exhausted = True
        return rows

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
        handle = ResultHandle(
            result_id=new_result_id(),
            cursor=cursor,
            columns=columns_from_description(getattr(cursor, "description", None)),
            total=_row_total(cursor),
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


def has_result_set(cursor: Any) -> bool:
    """True when the statement produced a grid rather than a status message."""
    description: Sequence[Any] | None = getattr(cursor, "description", None)
    return bool(description)
