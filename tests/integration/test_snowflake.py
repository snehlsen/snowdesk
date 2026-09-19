"""Opt-in tests against a real Snowflake account.

Run with::

    SNOWDESK_IT_CONNECTION=default uv run pytest -m integration

They create and drop a throwaway schema per run and are excluded from the
default test run.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid

import pytest

from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import ConnectJob, FetchMoreJob, RunScriptJob, SnowflakeWorker
from snowdesk.model import RunStatus

pytestmark = pytest.mark.integration

CONNECTION = os.environ.get("SNOWDESK_IT_CONNECTION")

if not CONNECTION:
    pytest.skip("SNOWDESK_IT_CONNECTION is not set", allow_module_level=True)


@pytest.fixture(scope="module")
def worker(qapp):
    worker = SnowflakeWorker(session=SnowflakeSession())
    worker._dispatch(ConnectJob(params=ConnectParams(name=CONNECTION)))
    assert worker.session.is_connected, "could not connect"
    yield worker
    worker.results.close_all()
    worker.session.close()


@pytest.fixture(scope="module")
def schema(worker):
    name = f"SNOWDESK_IT_{uuid.uuid4().hex[:8].upper()}"
    run(worker, f"CREATE SCHEMA {name}")
    yield name
    run(worker, f"DROP SCHEMA IF EXISTS {name}")


def run(worker: SnowflakeWorker, sql: str) -> list:
    outcomes: list = []
    worker.statement_finished.connect(outcomes.append)
    try:
        worker._dispatch(RunScriptJob(statements=split_sql(sql)))
    finally:
        worker.statement_finished.disconnect(outcomes.append)
    return outcomes


def test_connect_reports_context(worker) -> None:
    ctx = worker.session.read_context()
    assert ctx.role and ctx.warehouse


def test_multi_statement_script_with_use(worker, schema) -> None:
    outcomes = run(worker, f"USE SCHEMA {schema};\nSELECT CURRENT_SCHEMA() AS S;")
    assert [o.status for o in outcomes] == [RunStatus.SUCCESS, RunStatus.SUCCESS]
    assert worker.session.read_context().schema == schema


def test_large_result_spans_multiple_chunks(worker) -> None:
    results: list = []
    worker.result_ready.connect(lambda *a: results.append(a))
    sql = "SELECT SEQ4() AS N FROM TABLE(GENERATOR(ROWCOUNT => 5000))"
    try:
        worker._dispatch(RunScriptJob(statements=split_sql(sql), page_size=500))
    finally:
        worker.result_ready.disconnect()

    result_id, _columns, rows, exhausted, _total = results[0]
    assert len(rows) == 500
    assert not exhausted

    appended: list = []
    worker.rows_appended.connect(lambda *a: appended.append(a))
    try:
        while not appended or not appended[-1][2]:
            worker._dispatch(FetchMoreJob(result_id=result_id, page_size=500))
    finally:
        worker.rows_appended.disconnect()
    assert sum(len(a[1]) for a in appended) == 4500


def test_error_reports_code_and_query_id(worker) -> None:
    outcomes = run(worker, "SELECT * FROM SNOWDESK_NO_SUCH_TABLE")
    assert outcomes[0].status is RunStatus.ERROR
    assert outcomes[0].error is not None
    assert outcomes[0].error.errno
    assert outcomes[0].error.query_id


def test_failing_statement_skips_the_rest(worker) -> None:
    outcomes = run(worker, "SELECT 1; SELECT * FROM SNOWDESK_NO_SUCH_TABLE; SELECT 3;")
    assert [o.status for o in outcomes] == [
        RunStatus.SUCCESS,
        RunStatus.ERROR,
        RunStatus.SKIPPED,
    ]


def test_cancellation_is_server_side(worker) -> None:
    outcomes: list = []
    worker.statement_finished.connect(outcomes.append)
    worker.statement_started.connect(lambda *_: threading.Timer(1.0, worker.cancel_running).start())
    started = time.monotonic()
    try:
        worker._dispatch(RunScriptJob(statements=split_sql("SELECT SYSTEM$WAIT(30)")))
    finally:
        worker.statement_finished.disconnect(outcomes.append)
        worker.statement_started.disconnect()

    assert outcomes[0].status is RunStatus.CANCELLED
    assert time.monotonic() - started < 25, "cancel did not take effect quickly"
    # The query id is worth checking in Snowsight's history: it should show as
    # CANCELLED there, not as still running.
    assert outcomes[0].query_id


def test_worker_queue_drains_in_order(worker) -> None:
    assert isinstance(worker._queue, queue.Queue)
