"""Telling a dead connection from a bad statement (spec 9)."""

from __future__ import annotations

import pytest

from snowdesk.db.errors import is_session_lost
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import ConnectJob, ReconnectJob, RunScriptJob, SnowflakeWorker
from snowdesk.model import RunStatus
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStatement


class OperationalError(Exception):
    """Stands in for the connector's transport-level error."""


def collect(signal) -> list:
    received: list = []
    signal.connect(lambda *args: received.append(args if len(args) > 1 else args[0]))
    return received


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        FakeProgrammingError("Authentication token has expired", errno=390114),
        FakeProgrammingError("Session no longer exists", errno=390104),
        FakeProgrammingError("Connection is closed", errno=250002),
        FakeProgrammingError("could not connect", sqlstate="08001"),
        OperationalError("Max retries exceeded with url: ..."),
        ConnectionError("Connection reset by peer"),
        Exception("Temporary failure in name resolution"),
    ],
)
def test_transport_and_session_failures_are_recognised(exc: BaseException) -> None:
    assert is_session_lost(exc)


@pytest.mark.parametrize(
    "exc",
    [
        FakeProgrammingError("Object BOOM does not exist", errno=2003, sqlstate="42S02"),
        FakeProgrammingError("SQL compilation error", errno=1003),
        FakeProgrammingError("SQL execution canceled", errno=604),
        ValueError("something else entirely"),
    ],
)
def test_ordinary_failures_are_not_mistaken_for_a_lost_session(exc: BaseException) -> None:
    assert not is_session_lost(exc)


def test_a_cancel_is_never_a_lost_session() -> None:
    """Cancelling is deliberate; it must not tear the connection down."""
    assert not is_session_lost(FakeProgrammingError("SQL execution canceled", errno=604))


# -- worker behaviour -------------------------------------------------------


def _connected(qapp, plan=None):
    conn = FakeConnection(plan or {})
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    return worker, conn


def test_a_lost_session_disconnects_and_offers_reconnect(qapp) -> None:
    dead = OperationalError("Connection aborted")
    worker, _conn = _connected(qapp, {"select": FakeStatement(error=dead)})
    lost = collect(worker.connection_lost)
    states = collect(worker.state_changed)
    contexts = collect(worker.context_changed)

    worker._dispatch(RunScriptJob(statements=split_sql("select 1")))

    assert lost and lost[0][0] == "dev"
    assert not worker.session.is_connected  # the toolbar can no longer claim otherwise
    assert states[-1][0] == "error"
    assert contexts[-1].role is None


def test_the_failing_statement_still_reports_its_error(qapp) -> None:
    dead = OperationalError("Connection aborted")
    worker, _conn = _connected(qapp, {"select": FakeStatement(error=dead)})
    outcomes = collect(worker.statement_finished)
    worker._dispatch(RunScriptJob(statements=split_sql("select 1")))
    assert outcomes[0].status is RunStatus.ERROR
    assert "Connection aborted" in outcomes[0].message


def test_an_ordinary_sql_error_keeps_the_connection(qapp) -> None:
    boom = FakeProgrammingError("Object BOOM does not exist", errno=2003)
    worker, _conn = _connected(qapp, {"boom": FakeStatement(error=boom)})
    lost = collect(worker.connection_lost)

    worker._dispatch(RunScriptJob(statements=split_sql("select boom")))

    assert lost == []
    assert worker.session.is_connected


def test_reconnect_reopens_the_last_connection(qapp) -> None:
    attempts: list[str] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params.name)
        return FakeConnection()

    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=connect_fn))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    worker.session.close()  # as a lost session leaves things

    worker._dispatch(ReconnectJob())
    assert attempts == ["dev", "dev"]
    assert worker.session.is_connected


def test_reconnect_without_a_previous_connection_says_so(qapp) -> None:
    worker = SnowflakeWorker()
    errors = collect(worker.worker_error)
    worker._dispatch(ReconnectJob())
    assert errors == ["No connection to reconnect to."]


def test_a_lost_session_drops_live_results(qapp) -> None:
    """Cursors from a dead connection cannot be fetched from again."""
    dead = OperationalError("Connection reset by peer")
    cols = [("N", 0, None, None, 38, 0, False)]
    worker, conn = _connected(
        qapp, {"good": FakeStatement(columns=cols, rows=[(i,) for i in range(10)])}
    )
    worker._dispatch(RunScriptJob(statements=split_sql("select good"), page_size=5))
    assert len(worker.results) == 1

    conn.plan["good"] = FakeStatement(error=dead)
    worker._dispatch(RunScriptJob(statements=split_sql("select good")))
    assert len(worker.results) == 0
