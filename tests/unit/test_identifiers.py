from __future__ import annotations

import pytest

from snowdesk.db.identifiers import needs_quoting, qualify, quote_ident, quote_literal


@pytest.mark.parametrize(
    ("ident", "expected"),
    [
        ("ORDERS", "ORDERS"),
        ("ORDER_ITEMS", "ORDER_ITEMS"),
        ("_PRIVATE", "_PRIVATE"),
        ("T1", "T1"),
        ("A$B", "A$B"),
        ("orders", '"orders"'),
        ("Orders", '"Orders"'),
        ("my table", '"my table"'),
        ("1ST_TABLE", '"1ST_TABLE"'),
        ("", '""'),
        ('we"ird', '"we""ird"'),
        ("with.dot", '"with.dot"'),
    ],
)
def test_quote_ident(ident: str, expected: str) -> None:
    assert quote_ident(ident) == expected


def test_needs_quoting_matches_quote_ident() -> None:
    assert not needs_quoting("ORDERS")
    assert needs_quoting("orders")


def test_qualify_skips_empty_parts() -> None:
    assert qualify("RAW", "PUBLIC", "ORDERS") == "RAW.PUBLIC.ORDERS"
    assert qualify("RAW", None, "ORDERS") == "RAW.ORDERS"
    assert qualify("raw", "PUBLIC", "My Table") == '"raw".PUBLIC."My Table"'


def test_quote_literal_escapes() -> None:
    assert quote_literal("o'clock") == "'o''clock'"
    assert quote_literal("back\\slash") == "'back\\\\slash'"
