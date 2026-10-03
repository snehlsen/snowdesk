"""Stage jobs and transfers on the worker (docs/stage-browser.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowdesk.controllers.session_lifecycle import SessionLifecycle
from snowdesk.controllers.stages import StageController
from snowdesk.db.lanes import Lanes
from snowdesk.db.session import SnowflakeSession
from snowdesk.db.worker import ListStageJob, SnowflakeWorker, StagesJob
from snowdesk.model import FileStatus, StageFile, StageKind, StageRef, TransferKind
from snowdesk.storage.history import HistoryStore
from tests.fakes import (
    FakeConnection,
    FakeProgrammingError,
    FakeStages,
    start_session,
)

LANDING = StageRef(kind=StageKind.NAMED, name="LANDING", database="RAW", schema="PUBLIC")


@pytest.fixture
def worker_and_conn(qapp):
    conn = FakeConnection()
    conn.stage = FakeStages(
        stages=[{"name": "LANDING", "database_name": "RAW", "schema_name": "PUBLIC"}],
        files={"landing/a.csv.gz": b"a", "landing/b/c.csv.gz": b"c"},
    )
    worker = SnowflakeWorker(
        session=SnowflakeSession(connect_fn=lambda _p: conn), lanes=Lanes.synchronous()
    )
    lifecycle = SessionLifecycle(worker)
    start_session(lifecycle)
    return worker, conn, lifecycle


def collect(signal) -> list:
    received: list = []
    signal.connect(lambda *args: received.append(args if len(args) > 1 else args[0]))
    return received


def test_stages_job_lists_every_stage(worker_and_conn) -> None:
    worker, _conn, _lifecycle = worker_and_conn
    found = collect(worker.stages_ready)
    worker.submit(StagesJob())
    assert [s.name for s in found[0]] == ["LANDING"]


def test_list_stage_job_reports_files_and_truncation(worker_and_conn) -> None:
    worker, _conn, _lifecycle = worker_and_conn
    listed = collect(worker.stage_listed)
    worker.submit(ListStageJob(stage=LANDING, cap=1))
    stage, prefix, files, truncated = listed[0]
    assert stage == LANDING and prefix == "" and truncated
    assert [f.name for f in files] == ["a.csv.gz"]


def test_a_failed_listing_is_reported_against_its_node(worker_and_conn) -> None:
    worker, conn, _lifecycle = worker_and_conn
    conn.stage.fail_on = {}
    failures = collect(worker.stage_list_failed)

    def refuse(sql: str):
        raise FakeProgrammingError("Insufficient privileges to operate on stage", errno=3001)

    conn.stage.handle = refuse  # type: ignore[method-assign]
    worker.submit(ListStageJob(stage=LANDING, prefix="b/"))
    assert failures == [(LANDING, "b/", "[3001] Insufficient privileges to operate on stage")]


def test_an_unsafe_prefix_is_refused_without_a_query(worker_and_conn) -> None:
    worker, conn, _lifecycle = worker_and_conn
    failures = collect(worker.stage_list_failed)
    before = len(conn.executed)
    worker.submit(ListStageJob(stage=LANDING, prefix="it's/"))
    assert failures and "quote" in failures[0][2]
    assert len(conn.executed) == before


def test_controller_plans_confirms_and_records_history(worker_and_conn, tmp_path: Path) -> None:
    worker, _conn, lifecycle = worker_and_conn
    history = HistoryStore(tmp_path / "history.db")
    controller = StageController(worker, lifecycle, history=history)
    plans = collect(controller.plan_ready)
    finished = collect(controller.finished)
    local = tmp_path / "a.csv"
    local.write_text("new")

    controller.upload(LANDING, "", [str(local)])
    assert controller.is_busy
    (plan,) = plans
    assert plan.conflicts == ["a.csv.gz"]
    controller.upload(LANDING, "", [str(local)])  # refused: one at a time
    assert len(plans) == 1

    controller.confirm(plan, replace=True)
    assert not controller.is_busy
    (summary,) = finished
    assert summary.kind is TransferKind.UPLOAD
    assert summary.counts == {FileStatus.UPLOADED: 1}
    entries = history.recent()
    assert entries[0].sql.startswith("PUT ") and entries[0].connection == "dev"
    history.close()


def test_a_plan_that_fails_frees_the_controller(worker_and_conn, tmp_path: Path) -> None:
    worker, _conn, lifecycle = worker_and_conn
    controller = StageController(worker, lifecycle)
    failed = collect(controller.failed)
    external = StageRef(kind=StageKind.NAMED, name="X", database="D", schema="S", internal=False)
    controller.download(external, ["a"], str(tmp_path))
    assert not controller.is_busy
    controller.download(LANDING, ["it's/"], str(tmp_path))
    assert failed and "quote" in failed[0]
    assert not controller.is_busy


def test_an_unexpected_error_still_finishes_the_transfer(worker_and_conn, monkeypatch) -> None:
    worker, _conn, lifecycle = worker_and_conn
    controller = StageController(worker, lifecycle)
    finished = collect(controller.finished)

    def explode(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr("snowdesk.db.stages.run_transfer", explode)
    controller.remove(LANDING, [])  # nothing selected: no transfer at all
    assert finished == []
    controller.remove(LANDING, [StageFile(name="a.csv.gz", raw="landing/a.csv.gz")])
    assert finished and "bug" in finished[0].error
    assert not controller.is_busy
