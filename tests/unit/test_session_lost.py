"""Telling a dead connection from a bad statement (spec 9)."""

from __future__ import annotations

import pytest

from snowdesk.db.errors import is_session_lost
from snowdesk.db.lanes import Lanes
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import ConnectJob, RunScriptJob, SnowflakeWorker
from snowdesk.model import RunStatus
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStatement


class OperationalError(Exception):
    """Stands in for the connector's transport-level error."""

    def __init__(self, msg: str, errno: int | None = None) -> None:
        super().__init__(msg)
        self.errno = errno


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
        # PUT and GET errors are OperationalErrors too; the session is fine.
        OperationalError(
            "While getting file(s) there was an error: the file does not exist.", errno=253006
        ),
        OperationalError("While putting file(s) there was an error: ...", errno=253003),
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
    worker = SnowflakeWorker(
        session=SnowflakeSession(connect_fn=lambda _p: conn), lanes=Lanes.synchronous()
    )
    worker.submit(ConnectJob(params=ConnectParams(name="dev")))
    return worker, conn


def test_a_lost_session_is_reported_and_closed(qapp) -> None:
    dead = OperationalError("Connection aborted")
    worker, _conn = _connected(qapp, {"select": FakeStatement(error=dead)})
    lost = collect(worker.session_lost)

    worker.submit(RunScriptJob(statements=split_sql("select 1")))

    assert lost == [("dev", "Connection aborted")]
    # Closed on the worker thread, so the rest of the job fails fast.
    assert not worker.session.is_connected


def test_the_failing_statement_still_reports_its_error(qapp) -> None:
    dead = OperationalError("Connection aborted")
    worker, _conn = _connected(qapp, {"select": FakeStatement(error=dead)})
    outcomes = collect(worker.statement_finished)
    worker.submit(RunScriptJob(statements=split_sql("select 1")))
    assert outcomes[0].status is RunStatus.ERROR
    assert "Connection aborted" in outcomes[0].message


def test_an_ordinary_sql_error_keeps_the_connection(qapp) -> None:
    boom = FakeProgrammingError("Object BOOM does not exist", errno=2003)
    worker, _conn = _connected(qapp, {"boom": FakeStatement(error=boom)})
    lost = collect(worker.session_lost)

    worker.submit(RunScriptJob(statements=split_sql("select boom")))

    assert lost == []
    assert worker.session.is_connected


def test_a_lost_session_drops_live_results(qapp) -> None:
    """Cursors from a dead connection cannot be fetched from again."""
    dead = OperationalError("Connection reset by peer")
    cols = [("N", 0, None, None, 38, 0, False)]
    worker, conn = _connected(
        qapp, {"good": FakeStatement(columns=cols, rows=[(i,) for i in range(10)])}
    )
    worker.submit(RunScriptJob(statements=split_sql("select good"), page_size=5))
    assert len(worker.results) == 1

    conn.plan["good"] = FakeStatement(error=dead)
    worker.submit(RunScriptJob(statements=split_sql("select good")))
    assert len(worker.results) == 0


def test_an_export_that_loses_the_session_reports_it_but_leaves_closing_to_the_session_lane(
    qapp, tmp_path
) -> None:
    """The session belongs to the session lane; the Session lifecycle queues the close there."""
    cols = [("N", 0, None, None, 38, 0, False)]
    worker, conn = _connected(qapp, {"from orders": FakeStatement(columns=cols, rows=[(1,)])})
    ready = collect(worker.result_ready)
    worker.submit(RunScriptJob(statements=split_sql("select * from orders")))
    conn.plan["result_scan"] = FakeStatement(error=OperationalError("Connection aborted"))
    lost = collect(worker.session_lost)
    failed = collect(worker.export_failed)

    worker.export_csv(ready[0][0], str(tmp_path / "out.csv"), page_size=100)

    assert lost == [("dev", "Connection aborted")]
    assert failed and "Connection aborted" in failed[0][1]
    assert worker.session.is_connected
    assert not (tmp_path / "out.csv").exists()  # no partial file left behind


def test_an_export_without_a_session_is_refused(qapp, tmp_path) -> None:
    cols = [("N", 0, None, None, 38, 0, False)]
    worker, _conn = _connected(qapp, {"from orders": FakeStatement(columns=cols, rows=[(1,)])})
    ready = collect(worker.result_ready)
    worker.submit(RunScriptJob(statements=split_sql("select * from orders")))
    worker.session.close()  # the result handle outlives it until a Disconnect
    failed = collect(worker.export_failed)

    worker.export_csv(ready[0][0], str(tmp_path / "out.csv"), page_size=100)

    assert failed == [(ready[0][0], "Not connected.")]
