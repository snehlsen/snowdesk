from __future__ import annotations

import threading

import pytest

from snowdesk.db.errors import is_cancellation, to_query_error
from snowdesk.db.runner import Cancelled, StatementRunner
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.model import Statement
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStatement

COLS = [("N", 0, None, None, 38, 0, False)]


def stmt(sql: str) -> Statement:
    return Statement(sql=sql, start=0, end=len(sql))


def test_runs_async_and_returns_results() -> None:
    conn = FakeConnection({"select 1": FakeStatement(columns=COLS, rows=[(1,)])})
    runner = StatementRunner(conn)
    seen: list[str | None] = []
    cursor = runner.run(stmt("select 1"), seen.append)
    assert cursor.fetchmany(10) == [(1,)]
    assert seen and seen[0].startswith("01b0-")


def test_polls_until_the_query_finishes() -> None:
    conn = FakeConnection({"slow": FakeStatement(columns=COLS, rows=[(1,)], polls=3)})
    runner = StatementRunner(conn)
    runner.run(stmt("select slow"), lambda _qid: None)
    assert conn._polled[runner.current_query_id] == 4


def test_failure_propagates_with_details() -> None:
    error = FakeProgrammingError("Object does not exist", errno=2003, sqlstate="42S02")
    conn = FakeConnection({"bad": FakeStatement(error=error, polls=1)})
    runner = StatementRunner(conn)
    with pytest.raises(FakeProgrammingError):
        runner.run(stmt("select bad"), lambda _qid: None)


def test_cancel_breaks_the_polling_loop_and_calls_snowflake() -> None:
    conn = FakeConnection({"wait": FakeStatement(columns=COLS, rows=[(1,)], polls=1000)})
    runner = StatementRunner(conn)
    started = threading.Event()

    def cancel_when_started(_qid: str | None) -> None:
        started.set()

    def canceller() -> None:
        started.wait(2)
        runner.cancel()

    thread = threading.Thread(target=canceller)
    thread.start()
    with pytest.raises(Cancelled):
        runner.run(stmt("select system$wait(60) as wait"), cancel_when_started)
    thread.join(2)
    assert conn.cancelled_ids == [runner.current_query_id]


def test_cancel_before_any_query_is_harmless() -> None:
    runner = StatementRunner(FakeConnection())
    assert runner.cancel() is None


def test_put_and_get_run_synchronously() -> None:
    conn = FakeConnection({"put": FakeStatement(rowcount=1)})
    runner = StatementRunner(conn)
    runner.run(
        Statement(sql="put file://a.csv @s", start=0, end=18, is_put_or_get=True), lambda _q: None
    )
    assert conn.executed == ["put file://a.csv @s"]
    assert not conn.pending  # nothing went through execute_async


def test_is_cancellation_recognises_snowflake_wording() -> None:
    assert is_cancellation(FakeProgrammingError("SQL execution canceled", errno=604))
    assert not is_cancellation(FakeProgrammingError("Syntax error", errno=1003))


def test_to_query_error_extracts_metadata() -> None:
    err = to_query_error(
        FakeProgrammingError("boom", errno=2003, sqlstate="42S02", sfqid="01b0-0009")
    )
    assert err.errno == 2003
    assert err.sqlstate == "42S02"
    assert err.query_id == "01b0-0009"
    assert "[2003]" in err.formatted()
    assert "01b0-0009" in err.formatted()


def test_session_reads_context_from_the_connection() -> None:
    conn = FakeConnection()
    session = SnowflakeSession(connect_fn=lambda _p: conn)
    ctx = session.connect(ConnectParams(name="dev"))
    assert (ctx.role, ctx.warehouse, ctx.database, ctx.schema) == (
        "ANALYST",
        "COMPUTE_WH",
        "RAW",
        "PUBLIC",
    )
    assert str(ctx) == "ANALYST · COMPUTE_WH · RAW.PUBLIC"


def test_session_falls_back_to_current_functions() -> None:
    conn = FakeConnection(
        {"CURRENT_ROLE": FakeStatement(columns=COLS * 4, rows=[("R", "W", "D", "S")])}
    )
    conn.role = conn.warehouse = conn.database = conn.schema = None
    session = SnowflakeSession(connect_fn=lambda _p: conn)
    ctx = session.connect(ConnectParams(name="dev"))
    assert (ctx.role, ctx.warehouse, ctx.database, ctx.schema) == ("R", "W", "D", "S")


def test_reconnect_reuses_parameters() -> None:
    seen: list[ConnectParams] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        seen.append(params)
        return FakeConnection()

    session = SnowflakeSession(connect_fn=connect_fn)
    session.connect(ConnectParams(name="dev", role="ANALYST"))
    session.reconnect()
    assert [p.name for p in seen] == ["dev", "dev"]
    assert seen[1].role == "ANALYST"


def test_connect_failure_sets_error_state() -> None:
    def boom(_params: ConnectParams) -> FakeConnection:
        raise FakeProgrammingError("Incorrect username or password", errno=390100)

    session = SnowflakeSession(connect_fn=boom)
    with pytest.raises(FakeProgrammingError):
        session.connect(ConnectParams(name="dev"))
    assert session.state.value == "error"
    assert not session.is_connected
