from __future__ import annotations

from snowdesk.db.splitter import scan_statements, split_sql


def sqls(text: str) -> list[str]:
    return [s.sql for s in scan_statements(text)]


def test_simple_split() -> None:
    assert sqls("select 1; select 2;") == ["select 1", "select 2"]


def test_trailing_statement_without_semicolon() -> None:
    assert sqls("select 1;\nselect 2") == ["select 1", "select 2"]


def test_semicolon_inside_string_is_not_a_separator() -> None:
    assert sqls("select ';' as x; select 2") == ["select ';' as x", "select 2"]


def test_doubled_quote_inside_string() -> None:
    assert sqls("select 'it''s; fine' as x") == ["select 'it''s; fine' as x"]


def test_semicolon_inside_dollar_block() -> None:
    text = "create function f() as $$ select 1; select 2 $$; select 3"
    assert sqls(text) == ["create function f() as $$ select 1; select 2 $$", "select 3"]


def test_line_comment_hides_semicolon() -> None:
    assert sqls("select 1 -- ; not a split\n; select 2") == [
        "select 1 -- ; not a split",
        "select 2",
    ]


def test_block_comment_hides_semicolon() -> None:
    assert sqls("select /* ; */ 1; select 2") == ["select /* ; */ 1", "select 2"]


def test_quoted_identifier_with_semicolon() -> None:
    assert sqls('select "we;ird" from t; select 2') == ['select "we;ird" from t', "select 2"]


def test_blank_statements_are_dropped() -> None:
    assert sqls(";;\n  ;\nselect 1;") == ["select 1"]


def test_offsets_point_at_the_statement() -> None:
    text = "select 1;\n\nselect 2;"
    statements = scan_statements(text)
    assert [text[s.start : s.end] for s in statements] == ["select 1", "select 2"]


def test_put_and_get_are_flagged() -> None:
    statements = scan_statements("put file://a.csv @stage; select 1")
    assert statements[0].is_put_or_get
    assert not statements[1].is_put_or_get


def test_split_sql_offsets_round_trip() -> None:
    text = "use role ANALYST;\nselect 1;\nselect 'a;b';"
    for stmt in split_sql(text):
        assert text[stmt.start : stmt.end] == stmt.sql
