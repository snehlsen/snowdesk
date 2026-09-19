"""Snowflake identifier quoting (spec 7.7).

Snowflake folds unquoted identifiers to upper case, so an identifier only needs
quoting when it is *not* already an upper-case alphanumeric/underscore word.
Getting this wrong silently targets the wrong object, so it lives in one place
and is unit-tested.
"""

from __future__ import annotations

import re

_SAFE = re.compile(r"^[A-Z_][A-Z0-9_$]*$")


def needs_quoting(ident: str) -> bool:
    return not bool(_SAFE.match(ident))


def quote_ident(ident: str) -> str:
    """Quote ``ident`` only if it needs it, doubling any embedded quotes."""
    if not ident:
        return '""'
    if not needs_quoting(ident):
        return ident
    escaped = ident.replace('"', '""')
    return f'"{escaped}"'


def qualify(*parts: str | None) -> str:
    """Build a dotted, correctly quoted fully qualified name."""
    return ".".join(quote_ident(p) for p in parts if p)


def quote_literal(value: str) -> str:
    """Quote a string literal for embedding in a ``SHOW ... LIKE`` clause."""
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"
