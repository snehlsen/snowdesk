"""Statements the object browser context menu builds (B4)."""

from __future__ import annotations

from snowdesk.db import browser as browse


def test_preview_limits_rows() -> None:
    assert browse.preview_sql("RAW", "PUBLIC", "ORDERS") == (
        "SELECT * FROM RAW.PUBLIC.ORDERS LIMIT 100"
    )
    assert browse.preview_sql("RAW", "PUBLIC", "ORDERS", limit=5).endswith("LIMIT 5")


def test_preview_quotes_only_what_needs_it() -> None:
    """The spec's M5 exit criterion: mixed-case names must still resolve."""
    assert browse.preview_sql("raw", "PUBLIC", "My Table") == (
        'SELECT * FROM "raw".PUBLIC."My Table" LIMIT 100'
    )


def test_preview_limit_cannot_carry_sql() -> None:
    """The limit is forced through int(), so it cannot smuggle anything."""
    assert browse.preview_sql("D", "S", "T", limit=100).endswith("LIMIT 100")


def test_select_falls_back_to_star() -> None:
    assert browse.select_sql("RAW", "PUBLIC", "ORDERS") == "SELECT *\nFROM RAW.PUBLIC.ORDERS"
    assert browse.select_sql("RAW", "PUBLIC", "ORDERS", []) == "SELECT *\nFROM RAW.PUBLIC.ORDERS"


def test_select_lists_loaded_columns() -> None:
    sql = browse.select_sql("RAW", "PUBLIC", "ORDERS", ["ORDER_ID", "AMOUNT"])
    assert sql == "SELECT ORDER_ID,\n       AMOUNT\nFROM RAW.PUBLIC.ORDERS"


def test_select_quotes_awkward_column_names() -> None:
    sql = browse.select_sql("RAW", "PUBLIC", "ORDERS", ["ID", "order date", "Mixed"])
    assert sql.startswith('SELECT ID,\n       "order date",\n       "Mixed"\n')


def test_get_ddl_passes_the_name_as_a_literal() -> None:
    assert browse.get_ddl_sql(browse.TABLE, "RAW", "PUBLIC", "ORDERS") == (
        "SELECT GET_DDL('TABLE', 'RAW.PUBLIC.ORDERS')"
    )


def test_get_ddl_knows_each_object_type() -> None:
    assert "'VIEW'" in browse.get_ddl_sql(browse.VIEW, "D", "S", "V")
    assert "'SCHEMA'" in browse.get_ddl_sql(browse.SCHEMA, "D", "S")
    assert "'DATABASE'" in browse.get_ddl_sql(browse.DATABASE, "D")


def test_get_ddl_keeps_identifier_quoting_inside_the_literal() -> None:
    """A mixed-case name only resolves if its quotes survive into the string."""
    assert browse.get_ddl_sql(browse.VIEW, "RAW", "PUBLIC", "V_Orders") == (
        """SELECT GET_DDL('VIEW', 'RAW.PUBLIC."V_Orders"')"""
    )


def test_get_ddl_escapes_a_quote_in_the_name() -> None:
    sql = browse.get_ddl_sql(browse.TABLE, "RAW", "PUBLIC", "we'ird")
    # The apostrophe is doubled for the literal, inside an identifier-quoted name.
    assert sql == """SELECT GET_DDL('TABLE', 'RAW.PUBLIC."we''ird"')"""


def test_an_unknown_kind_falls_back_to_table() -> None:
    assert "'TABLE'" in browse.get_ddl_sql("something-else", "D", "S", "T")
