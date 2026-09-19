"""Telling a grid apart from a status line (Q6)."""

from __future__ import annotations

import pytest

from snowdesk.db.results import ResultRegistry, leading_keyword, summarize_status
from snowdesk.model import ColumnInfo


def cols(*names: str) -> list[ColumnInfo]:
    return [ColumnInfo(n, "TEXT") for n in names]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select 1", "SELECT"),
        ("  \n select 1", "SELECT"),
        ("-- a comment\nselect 1", "SELECT"),
        ("// a comment\nselect 1", "SELECT"),
        ("/* block */ select 1", "SELECT"),
        ("/* multi\nline */\nwith t as (select 1) select * from t", "WITH"),
        ("(select 1)", "SELECT"),
        ("use role ANALYST", "USE"),
        ("create or replace table t (a int)", "CREATE"),
        ("", ""),
        ("-- only a comment", ""),
    ],
)
def test_leading_keyword(sql: str, expected: str) -> None:
    assert leading_keyword(sql) == expected


def test_ddl_status_becomes_a_message() -> None:
    summary = summarize_status(
        cols("status"), [("Table T successfully created.",)], "create table t (a int)"
    )
    assert summary == "Table T successfully created."


def test_session_command_becomes_a_message() -> None:
    summary = summarize_status(
        cols("status"), [("Statement executed successfully.",)], "use role ANALYST"
    )
    assert summary == "Statement executed successfully."


def test_insert_counter_becomes_a_message() -> None:
    summary = summarize_status(cols("number of rows inserted"), [(7,)], "insert into t values (1)")
    assert summary == "7 rows inserted"


def test_merge_reports_every_counter() -> None:
    summary = summarize_status(
        cols("number of rows inserted", "number of rows updated"),
        [(2, 5)],
        "merge into t using s on t.id = s.id",
    )
    assert summary == "2 rows inserted, 5 rows updated"


def test_select_always_gets_a_grid() -> None:
    assert summarize_status(cols("N"), [(1,)], "select 1 as n") is None


def test_a_query_whose_shape_mimics_a_status_still_gets_a_grid() -> None:
    """The leading keyword is what saves `select status from t limit 1`."""
    assert summarize_status(cols("status"), [("shipped",)], "select status from t limit 1") is None
    assert (
        summarize_status(cols("status"), [("x",)], "with t as (select 1) select status from t")
        is None
    )


def test_show_and_describe_get_a_grid() -> None:
    assert summarize_status(cols("status"), [("x",)], "show tables") is None
    assert summarize_status(cols("status"), [("x",)], "describe table t") is None


def test_copy_into_keeps_its_grid() -> None:
    """COPY reports per-file rows, which are worth seeing in full."""
    summary = summarize_status(
        cols("file", "status", "rows_parsed"), [("a.csv", "LOADED", 10)], "copy into t from @s"
    )
    assert summary is None


def test_multi_row_non_query_results_keep_their_grid() -> None:
    assert summarize_status(cols("status"), [("a",), ("b",)], "call p()") is None
    assert summarize_status(cols("status"), [("a",), ("b",)], "alter table t ...") is None


def test_statement_with_no_columns_is_a_bare_success() -> None:
    assert summarize_status([], [], "grant select on t to role r") == "Statement executed."


def test_registry_reads_metadata_only_after_the_first_fetch() -> None:
    """Mirrors the connector: an async cursor has no description until fetched."""

    class LazyCursor:
        description = None
        rowcount = -1

        def fetchmany(self, size: int) -> list[tuple]:
            type(self).description = [("N", 0, None, None, 38, 0, False)]
            type(self).rowcount = 1
            return [(1,)]

        def close(self) -> None:
            pass

    registry = ResultRegistry()
    handle = registry.register(LazyCursor())
    assert handle.columns == []
    assert not handle.primed

    rows = handle.fetch(500)
    assert rows == [(1,)]
    assert handle.primed
    assert [c.name for c in handle.columns] == ["N"]
    assert handle.total == 1
