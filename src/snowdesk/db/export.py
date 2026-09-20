"""Streaming a full result to CSV (R6).

The grid's cursor has usually been partly consumed by the time an export is
asked for, and may have stopped at the row cap, so draining it would export
neither the whole result nor leave the grid usable.  The result is re-read with
``RESULT_SCAN`` instead: a fresh cursor over the same query, complete
regardless of what the grid holds, repeatable, and leaving the grid alone.

Rows are written a page at a time and never accumulated, so the memory cost of
exporting a million rows is the same as exporting a thousand.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from snowdesk.db.session import Connection
from snowdesk.model import ColumnInfo, columns_from_description
from snowdesk.util.export import write_csv

log = logging.getLogger(__name__)

#: How often to report progress, in rows.  Frequent enough for the count to
#: move, rare enough not to flood the UI thread with signals.
PROGRESS_EVERY = 5_000


class ExportCancelled(Exception):
    """Raised inside the export thread when the user asks it to stop."""


def result_scan_sql() -> str:
    """Re-read a finished query's result by id.

    The id is bound rather than interpolated: it comes from the server, but a
    query id has no business being pasted into SQL text.
    """
    return "SELECT * FROM TABLE(RESULT_SCAN(%s))"


def export_result(
    conn: Connection,
    query_id: str,
    path: Path | str,
    page_size: int,
    cancelled: threading.Event,
    on_progress: Callable[[int], None] | None = None,
    escape_formulas: bool = True,
) -> int:
    """Stream the whole result of ``query_id`` to ``path``; return the row count."""
    cursor = conn.cursor()
    try:
        cursor.execute(result_scan_sql(), (query_id,))
        columns = columns_from_description(getattr(cursor, "description", None))
        written = 0

        def batches() -> Iterator[list[tuple[Any, ...]]]:
            nonlocal written
            last_reported = 0
            while True:
                if cancelled.is_set():
                    raise ExportCancelled
                rows = cursor.fetchmany(page_size)
                if not rows:
                    return
                yield [tuple(row) for row in rows]
                written += len(rows)
                if on_progress and written - last_reported >= PROGRESS_EVERY:
                    last_reported = written
                    on_progress(written)

        target = Path(path)
        with target.open("w", encoding="utf-8", newline="") as handle:
            total = write_csv(handle, columns, batches(), escape_formulas=escape_formulas)
        if on_progress:
            on_progress(total)
        return total
    finally:
        try:
            cursor.close()
        except Exception:
            log.debug("Ignoring error closing export cursor", exc_info=True)


def discard(path: Path | str) -> None:
    """Remove a half-written file after a cancelled or failed export."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove partial export %s", path, exc_info=True)


def default_filename(columns: list[ColumnInfo] | None = None) -> str:
    return "result.csv"
