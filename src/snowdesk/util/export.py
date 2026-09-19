"""Clipboard TSV (R5) and CSV export helpers."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

from snowdesk.model import ColumnInfo
from snowdesk.util.formatting import format_cell


def _escape_tsv(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "")


def rows_to_tsv(
    rows: Iterable[Sequence[Any]],
    columns: Sequence[ColumnInfo] | None = None,
    *,
    with_headers: bool = False,
    column_indexes: Sequence[int] | None = None,
) -> str:
    """Render rows as TSV for the clipboard.

    ``column_indexes`` selects and orders the columns to include, matching a
    rectangular selection in the grid.
    """
    out: list[str] = []
    if with_headers and columns is not None:
        idx = column_indexes if column_indexes is not None else range(len(columns))
        out.append("\t".join(_escape_tsv(columns[i].name) for i in idx))
    for row in rows:
        idx = column_indexes if column_indexes is not None else range(len(row))
        cells = []
        for i in idx:
            col = columns[i] if columns is not None and i < len(columns) else None
            cells.append(_escape_tsv(format_cell(row[i], col)))
        out.append("\t".join(cells))
    return "\n".join(out)


def write_csv(
    handle: io.TextIOBase,
    columns: Sequence[ColumnInfo],
    row_batches: Iterable[Iterable[Sequence[Any]]],
    *,
    with_headers: bool = True,
) -> int:
    """Stream row batches to a CSV file handle, returning the row count.

    Batches are consumed lazily so a large export never holds the full result
    in memory (R6).  NULL is written as an empty field, which is what
    spreadsheets expect; the empty string round-trips as an empty field too, a
    known and conventional CSV ambiguity.
    """
    writer = csv.writer(handle)
    if with_headers:
        writer.writerow([c.name for c in columns])
    written = 0
    for batch in row_batches:
        for row in batch:
            writer.writerow(
                [
                    ""
                    if value is None
                    else format_cell(value, columns[i] if i < len(columns) else None)
                    for i, value in enumerate(row)
                ]
            )
            written += 1
    return written
