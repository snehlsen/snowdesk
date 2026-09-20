"""Clipboard TSV (R5) and CSV export helpers."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

from snowdesk.model import ColumnInfo
from snowdesk.util.formatting import format_cell

#: Leading characters that make Excel and Google Sheets treat a field as a
#: formula rather than as text.  Tab and carriage return are in the list
#: because they are stripped before that decision is made, so they hide a
#: leading ``=`` from a naive check.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formula(text: str) -> str:
    """Stop a spreadsheet from executing a value that came out of the database.

    A field opening with ``=``, ``+``, ``-`` or ``@`` is evaluated on open, so
    whoever can write a row into a table can put code on the machine of every
    analyst who exports it: ``=WEBSERVICE(...)`` posts the neighbouring cells
    to a URL, and the DDE form tries to launch a program.  Nothing about the
    value looks dangerous in the grid, which is what makes it worth handling
    at the point the data leaves for a spreadsheet.

    A leading apostrophe is the convention both Excel and Sheets read as "this
    is text".  It is visible in the file, which is the cost, and why the
    caller can turn it off.
    """
    return "'" + text if text.startswith(_FORMULA_LEAD) else text


def _escape_tsv(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "")


def rows_to_tsv(
    rows: Iterable[Sequence[Any]],
    columns: Sequence[ColumnInfo] | None = None,
    *,
    with_headers: bool = False,
    column_indexes: Sequence[int] | None = None,
    escape_formulas: bool = True,
) -> str:
    """Render rows as TSV for the clipboard.

    ``column_indexes`` selects and orders the columns to include, matching a
    rectangular selection in the grid.  ``escape_formulas`` applies
    :func:`neutralize_formula`, because the usual destination for a copied
    selection is the same spreadsheet an export would open in.
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
            text = _escape_tsv(format_cell(row[i], col))
            cells.append(neutralize_formula(text) if escape_formulas else text)
        out.append("\t".join(cells))
    return "\n".join(out)


def write_csv(
    handle: io.TextIOBase,
    columns: Sequence[ColumnInfo],
    row_batches: Iterable[Iterable[Sequence[Any]]],
    *,
    with_headers: bool = True,
    escape_formulas: bool = True,
) -> int:
    """Stream row batches to a CSV file handle, returning the row count.

    Batches are consumed lazily so a large export never holds the full result
    in memory (R6).  NULL is written as an empty field, which is what
    spreadsheets expect; the empty string round-trips as an empty field too, a
    known and conventional CSV ambiguity.

    ``escape_formulas`` prefixes values a spreadsheet would otherwise execute;
    see :func:`neutralize_formula`.  Column names go through it too, since a
    ``SELECT ... AS "=..."`` names its own header.
    """
    writer = csv.writer(handle)
    guard = neutralize_formula if escape_formulas else _unchanged
    if with_headers:
        writer.writerow([guard(c.name) for c in columns])
    written = 0
    for batch in row_batches:
        for row in batch:
            writer.writerow(
                [
                    ""
                    if value is None
                    else guard(format_cell(value, columns[i] if i < len(columns) else None))
                    for i, value in enumerate(row)
                ]
            )
            written += 1
    return written


def _unchanged(text: str) -> str:
    return text
