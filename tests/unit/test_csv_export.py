"""Streaming a full result to CSV (R6)."""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from snowdesk.db import export as csv_export


class FakeExportCursor:
    """Hands out rows a page at a time and counts what it was asked for."""

    def __init__(self, rows: int, columns=None) -> None:
        self.description = columns or [("N", 0, None, None, 38, 0, False)]
        self._remaining = rows
        self._next = 0
        self.executed: list[tuple] = []
        self.closed = False
        self.largest_page = 0

    def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return self

    def fetchmany(self, size: int):
        self.largest_page = max(self.largest_page, size)
        taken = min(size, self._remaining)
        rows = [(self._next + i,) for i in range(taken)]
        self._next += taken
        self._remaining -= taken
        return rows

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def export(tmp_path: Path, rows: int, page_size: int = 500, **kwargs):
    cursor = FakeExportCursor(rows)
    path = tmp_path / "out.csv"
    written = csv_export.export_result(
        FakeConn(cursor), "01b0-0001", path, page_size, threading.Event(), **kwargs
    )
    return cursor, path, written


# -- the full result, not the loaded rows -----------------------------------


def test_the_result_is_re_read_by_query_id(tmp_path: Path) -> None:
    """The grid's cursor is partly consumed and may have stopped at the cap."""
    cursor, _path, _written = export(tmp_path, 3)
    sql, params = cursor.executed[0]
    assert "RESULT_SCAN" in sql.upper()
    assert params == ("01b0-0001",)
    # Bound, not pasted into the statement text.
    assert "01b0-0001" not in sql


def test_every_row_is_written_with_a_header(tmp_path: Path) -> None:
    _cursor, path, written = export(tmp_path, 2_500)
    assert written == 2_500
    with path.open() as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["N"]
    assert len(rows) == 2_501
    assert rows[1] == ["0"] and rows[-1] == ["2499"]


def test_rows_are_never_all_held_at_once(tmp_path: Path) -> None:
    """The exit criterion: a huge export must not accumulate rows."""
    cursor, _path, written = export(tmp_path, 250_000, page_size=1_000)
    assert written == 250_000
    # Nothing ever asked for more than one page.
    assert cursor.largest_page == 1_000


def test_an_empty_result_still_gets_its_header(tmp_path: Path) -> None:
    _cursor, path, written = export(tmp_path, 0)
    assert written == 0
    assert path.read_text().strip() == "N"


def test_the_cursor_is_closed_afterwards(tmp_path: Path) -> None:
    cursor, _path, _written = export(tmp_path, 10)
    assert cursor.closed


# -- progress and cancelling ------------------------------------------------


def test_progress_is_reported_as_it_goes(tmp_path: Path) -> None:
    seen: list[int] = []
    _cursor, _path, written = export(tmp_path, 20_000, page_size=1_000, on_progress=seen.append)
    assert seen, "no progress was reported"
    assert seen == sorted(seen)
    assert seen[-1] == written


def test_cancelling_stops_and_removes_the_partial_file(tmp_path: Path) -> None:
    cursor = FakeExportCursor(1_000_000)
    path = tmp_path / "out.csv"
    stop = threading.Event()

    def cancel_after_a_bit(written: int) -> None:
        if written >= 10_000:
            stop.set()

    with pytest.raises(csv_export.ExportCancelled):
        csv_export.export_result(
            FakeConn(cursor), "q", path, 1_000, stop, on_progress=cancel_after_a_bit
        )
    csv_export.discard(path)
    assert not path.exists()


def test_discard_is_safe_when_there_is_no_file(tmp_path: Path) -> None:
    csv_export.discard(tmp_path / "never-written.csv")  # must not raise
