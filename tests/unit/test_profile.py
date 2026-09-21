"""Query-id validation and the GET_QUERY_OPERATOR_STATS statement."""

from __future__ import annotations

import pytest

from snowdesk.db import profile

QID = "01b8e0f5-0000-d7a5-0000-a3bd0003e0ba"


@pytest.mark.parametrize("value", [QID, QID.upper(), f"  {QID}  ", "01b0-0001"])
def test_query_ids_are_accepted(value: str) -> None:
    assert profile.is_query_id(value)


@pytest.mark.parametrize("value", [None, "", "   ", "select 1", "01b8e0f5-0000-zzzz", "ab"])
def test_non_query_ids_are_rejected(value: str | None) -> None:
    assert not profile.is_query_id(value)


def test_profile_sql_quotes_the_id_and_orders_the_plan() -> None:
    sql = profile.profile_sql(f"  {QID}  ")
    assert f"GET_QUERY_OPERATOR_STATS('{QID}')" in sql
    assert sql.endswith("ORDER BY STEP_ID, OPERATOR_ID")


def test_profile_sql_escapes_a_quote() -> None:
    """Nothing reaches here unvalidated, but the literal is still escaped."""
    assert "'a''b'" in profile.profile_sql("a'b")


def test_short_id_is_the_leading_segment() -> None:
    assert profile.short_id(QID) == "01b8e0f5"


def test_empty_hint_names_the_query_and_the_usual_causes() -> None:
    hint = profile.empty_hint(QID)
    assert QID in hint
    assert "cache" in hint
    assert "14 days" in hint
