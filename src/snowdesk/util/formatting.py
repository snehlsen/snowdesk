"""Cell rendering (R3).

NULL is rendered distinctly from the empty string, timestamps keep their time
zone, and VARIANT/OBJECT/ARRAY collapse to compact JSON.

:func:`format_cell` is the value as text, line breaks and all; the grid shows
:func:`display_cell`, which fits that on one line, and the detail panel shows
:func:`detail_cell`, which spreads JSON out over as many as it needs.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from functools import lru_cache
from typing import Any

from snowdesk.model import ColumnInfo

#: Sentinel text shown for SQL NULL.  The view additionally styles NULL cells
#: (italic, dimmed) so it cannot be confused with the literal string 'NULL'.
NULL_TEXT = "NULL"

#: Stands in for a line break in the grid, which has room for one line: Qt
#: would otherwise show the first line and elide the rest, so a pretty-printed
#: document reads as ``{...``.
LINE_BREAK_MARK = "\u21b5"


def _compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)


def format_json(value: Any) -> str:
    """Render a VARIANT/OBJECT/ARRAY value as compact one-line JSON."""
    if isinstance(value, str):
        try:
            return _compact_json(json.loads(value))
        except (ValueError, TypeError):
            return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return _compact_json(value)


def pretty_json(value: Any) -> str:
    """Indented JSON for the cell detail panel."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return str(value)
    try:
        return json.dumps(parsed, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _looks_like_json(text: str) -> bool:
    return text.lstrip().startswith(("{", "["))


@lru_cache(maxsize=256)
def _compact_json_text(text: str) -> str | None:
    """``text`` as compact JSON if it is a JSON object or array, else None.

    Cached because the grid asks for a cell's text on every paint, and a
    function like SYSTEM$CLUSTERING_INFORMATION returns a sizeable document.
    """
    if not _looks_like_json(text):
        return None
    try:
        return _compact_json(json.loads(text))
    except (ValueError, TypeError):
        return None


def _one_line(text: str) -> str:
    return (
        text.replace("\r\n", LINE_BREAK_MARK)
        .replace("\n", LINE_BREAK_MARK)
        .replace("\r", LINE_BREAK_MARK)
    )


def _format_datetime(value: dt.datetime) -> str:
    text = value.isoformat(sep=" ", timespec="microseconds")
    if value.tzinfo is not None and value.utcoffset() is not None:
        # isoformat already appends the offset; normalise +00:00 style.
        return text
    return text


def format_cell(value: Any, column: ColumnInfo | None = None) -> str:
    """Render one cell for display in the grid."""
    if value is None:
        return NULL_TEXT
    if column is not None and column.is_json:
        return format_json(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        return _format_datetime(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex().upper()
    if isinstance(value, (dict, list)):
        return _compact_json(value)
    return str(value)


def display_cell(value: Any, column: ColumnInfo | None = None) -> str:
    """Render one cell for the grid, which shows a single line of it.

    A VARCHAR holding a JSON object or array, the way the SYSTEM$ functions
    return theirs, is compacted like a VARIANT; any other line break becomes
    :data:`LINE_BREAK_MARK`.
    """
    text = format_cell(value, column)
    if isinstance(value, str) and not (column is not None and column.is_json):
        compact = _compact_json_text(text)
        if compact is not None:
            return compact
    return _one_line(text)


def detail_cell(value: Any, column: ColumnInfo | None = None) -> str:
    """Render one cell in full for the detail panel.

    JSON is pretty-printed, whether it arrived as a VARIANT or as a VARCHAR
    that holds a JSON object or array; everything else is the value as is.
    """
    if value is None:
        return NULL_TEXT
    if (column is not None and column.is_json) or (
        isinstance(value, str) and _compact_json_text(value) is not None
    ):
        return pretty_json(value)
    return format_cell(value, column)


def format_row_count(loaded: int, total: int | None, exhausted: bool) -> str:
    """Status-bar row counter: ``500 of ?`` while fetching, ``12,340 rows`` after."""
    if exhausted:
        return f"{loaded:,} row" + ("" if loaded == 1 else "s")
    if total is not None and total >= 0:
        return f"{loaded:,} of {total:,} rows"
    return f"{loaded:,} of ? rows"


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.1f}s"


def format_bytes(size: int) -> str:
    """``12.4 MB``: compact enough for the Stages sidebar, in decimal units as
    Finder shows them."""
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 999.95 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"  # unreachable; keeps the type checker content
