"""Statement splitting (Q2, spec 7.3).

The connector ships a splitter that already understands quotes, comments and
``$$``-delimited bodies and flags ``PUT``/``GET`` statements (which cannot run
through ``execute_async``).  We prefer it, but it returns only statement text,
so each statement is located back in the original buffer to recover the offsets
needed to highlight a failing statement (Q5).

A self-contained scanner is used when the connector is not importable, so the
splitting logic stays testable in isolation.
"""

from __future__ import annotations

import io

from snowdesk.model import Statement

_PUT_GET_PREFIXES = ("PUT ", "GET ", "PUT\t", "GET\t", "PUT\n", "GET\n")


def _is_put_or_get(sql: str) -> bool:
    stripped = sql.lstrip().upper()
    return stripped.startswith(_PUT_GET_PREFIXES)


def scan_statements(text: str) -> list[Statement]:
    """Split ``text`` on top-level semicolons, honouring quoting and comments.

    Understands ``'...'``, ``"..."``, ``$$...$$``, ``--`` / ``//`` line comments
    and ``/* ... */`` block comments (Snowflake block comments do not nest).
    """
    statements: list[Statement] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == '"':
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\" and quote == "'":
                    i += 2
                    continue
                if text[i] == quote:
                    # A doubled quote is an escaped quote, not a terminator.
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if text.startswith("$$", i):
            end = text.find("$$", i + 2)
            i = n if end == -1 else end + 2
            continue
        if text.startswith("--", i) or (text.startswith("//", i) and _starts_comment(text, i)):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == ";":
            _append(statements, text, start, i)
            i += 1
            start = i
            continue
        i += 1
    _append(statements, text, start, n)
    return statements


def _starts_comment(text: str, i: int) -> bool:
    """``//`` only opens a comment at a token boundary.

    Without this, the ``//`` in ``PUT file://...`` would swallow the rest of
    the line and lose the statements after it.
    """
    return i == 0 or text[i - 1].isspace() or text[i - 1] in ";,()"


def _append(out: list[Statement], text: str, start: int, end: int) -> None:
    chunk = text[start:end]
    stripped = chunk.strip()
    if not stripped:
        return
    offset = start + (len(chunk) - len(chunk.lstrip()))
    out.append(
        Statement(
            sql=stripped,
            start=offset,
            end=offset + len(stripped),
            is_put_or_get=_is_put_or_get(stripped),
        )
    )


def _locate(text: str, sql: str, from_index: int) -> tuple[int, int]:
    """Find ``sql`` in ``text`` at or after ``from_index``; fall back gracefully."""
    pos = text.find(sql, from_index)
    if pos == -1:
        head = sql.strip().split("\n", 1)[0]
        pos = text.find(head, from_index) if head else -1
        if pos == -1:
            return from_index, min(from_index + len(sql), len(text))
    return pos, pos + len(sql)


def split_sql(text: str) -> list[Statement]:
    """Split editor text into executable statements with source offsets."""
    try:
        from snowflake.connector.util_text import split_statements
    except ImportError:
        return scan_statements(text)

    statements: list[Statement] = []
    cursor = 0
    try:
        chunks = list(split_statements(io.StringIO(text)))
    except Exception:
        return scan_statements(text)

    for sql, is_put_get in chunks:
        if sql is None:
            continue
        stripped = sql.strip()
        if not stripped or stripped == ";":
            continue
        stripped = stripped.rstrip(";").strip()
        if not stripped:
            continue
        start, end = _locate(text, stripped, cursor)
        cursor = end
        statements.append(
            Statement(
                sql=stripped,
                start=start,
                end=end,
                is_put_or_get=bool(is_put_get) or _is_put_or_get(stripped),
            )
        )
    return statements
