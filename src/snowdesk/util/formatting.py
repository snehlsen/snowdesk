"""Cell rendering (R3).

NULL is rendered distinctly from the empty string, timestamps keep their time
zone, and VARIANT/OBJECT/ARRAY collapse to compact JSON.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

from snowdesk.model import ColumnInfo

#: Sentinel text shown for SQL NULL.  The view additionally styles NULL cells
#: (italic, dimmed) so it cannot be confused with the literal string 'NULL'.
NULL_TEXT = "NULL"


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
