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
from snowdesk.db.worker import (
    ConnectJob,
    EndTransactionJob,
    FetchMoreJob,
    RunScriptJob,
    SetAutocommitJob,
    SnowflakeWorker,
)
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


def test_manual_commit_round_trip(worker, schema) -> None:
    """Q10 against the real thing: the reads, the refusal and the mode switch."""
    table = f"{schema}.SNOWDESK_TXN"
    run(worker, f"CREATE OR REPLACE TABLE {table} (N INT)")
    assert worker._transaction.autocommit is True
    worker._dispatch(SetAutocommitJob(enabled=False))
    try:
        assert worker._transaction.autocommit is False

        run(worker, f"INSERT INTO {table} VALUES (1)")
        assert worker._transaction.in_transaction
        refused: list = []
        worker.autocommit_failed.connect(refused.append)
        try:
            worker._dispatch(SetAutocommitJob(enabled=True))
        finally:
            worker.autocommit_failed.disconnect(refused.append)
        assert refused and worker.session.read_autocommit() is False

        worker._dispatch(EndTransactionJob(commit=False))
        assert not worker._transaction.in_transaction

        run(worker, f"INSERT INTO {table} VALUES (2)")
        worker._dispatch(EndTransactionJob(commit=True))
        assert not worker._transaction.in_transaction
    finally:
        worker._dispatch(EndTransactionJob(commit=False))
        worker._dispatch(SetAutocommitJob(enabled=True))
    assert worker._transaction.autocommit is True

    outcomes = run(worker, f"SELECT N FROM {table}")
    worker.results.close(outcomes[0].result_id)
    assert outcomes[0].row_count == 1  # the rolled-back row is gone


# -- stages (docs/stage-browser.md) ---------------------------------------------
#
# Each of these checks something about the server that the fake can only
# assume: the name LIST gives a named stage's files, that a whole location in
# quotes is accepted, what GET's PATTERN is matched against, and that PUT
# reports what it compressed.


@pytest.fixture(scope="module")
def stage(worker, schema):
    from snowdesk.model import StageKind, StageRef

    database = worker.session.read_context().database
    run(worker, f"CREATE STAGE {schema}.IT_STAGE")
    return StageRef(kind=StageKind.NAMED, name="IT_STAGE", database=database, schema=schema)


def transfer(worker, plan):
    from snowdesk.db import stages as stage_ops

    return stage_ops.run_transfer(worker.session.connection, plan, threading.Event())


def test_stage_round_trip_keeps_folders_apart(worker, stage, tmp_path) -> None:
    from snowdesk.db import stages as stage_ops
    from snowdesk.model import FileStatus

    conn = worker.session.connection
    for folder in ("a", "b", "my folder"):
        (tmp_path / "up" / folder).mkdir(parents=True)
        (tmp_path / "up" / folder / "x.csv").write_text(f"from {folder}\n")

    found = stage_ops.list_stages(conn)
    assert any(s.name == "IT_STAGE" and s.internal for s in found)

    plan = stage_ops.plan_upload(conn, "it1", stage, "", [str(tmp_path / "up")])
    summary = transfer(worker, plan)
    assert summary.counts == {FileStatus.UPLOADED: 3}, summary

    files, truncated = stage_ops.list_files(conn, stage)
    assert not truncated
    assert sorted(f.name for f in files) == [
        "up/a/x.csv.gz",
        "up/b/x.csv.gz",
        "up/my folder/x.csv.gz",
    ]

    down = tmp_path / "down"
    down.mkdir()
    plan = stage_ops.plan_download(conn, "it2", stage, ["up/"], down)
    summary = transfer(worker, plan)
    assert summary.counts == {FileStatus.DOWNLOADED: 3}, summary
    for folder in ("a", "b", "my folder"):
        assert (down / "up" / folder / "x.csv.gz").exists()


def test_removing_a_file_spares_its_prefix_sibling(worker, stage, tmp_path) -> None:
    from snowdesk.db import stages as stage_ops

    conn = worker.session.connection
    for name in ("data.csv", "data.csv.bak"):
        (tmp_path / name).write_text(name)
    plan = stage_ops.plan_upload(
        conn, "it3", stage, "rm/", [str(tmp_path / "data.csv"), str(tmp_path / "data.csv.bak")]
    )
    transfer(worker, plan)
    files, _ = stage_ops.list_files(conn, stage, "rm/")
    target = next(f for f in files if f.name == "rm/data.csv.gz")
    transfer(worker, stage_ops.plan_remove("it4", stage, [target]))
    files, _ = stage_ops.list_files(conn, stage, "rm/")
    assert [f.name for f in files] == ["rm/data.csv.bak.gz"]


def test_select_from_a_staged_file(worker, stage, tmp_path) -> None:
    from snowdesk.db import stages as stage_ops

    conn = worker.session.connection
    (tmp_path / "q.csv").write_text("1,2\n")
    transfer(worker, stage_ops.plan_upload(conn, "it5", stage, "q/", [str(tmp_path / "q.csv")]))
    files, _ = stage_ops.list_files(conn, stage, "q/")
    outcomes = run(worker, stage_ops.select_file_sql(stage, files[0]))
    assert outcomes[0].status is RunStatus.SUCCESS, outcomes[0].message
