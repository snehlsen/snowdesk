"""The worker's execution lanes: the threaded adapter and the synchronous one."""

from __future__ import annotations

import threading

import pytest

from snowdesk.db.lanes import InlineLane, QueueLane
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import ConnectJob, RunScriptJob, SnowflakeWorker
from tests.fakes import FakeConnection

# -- the synchronous lane -------------------------------------------------------


def test_inline_lane_runs_work_as_it_is_submitted() -> None:
    lane = InlineLane()
    ran: list[str] = []
    lane.submit(lambda: ran.append("a"))
    assert ran == ["a"]


def test_work_submitted_by_running_work_waits_for_it() -> None:
    """As it would behind a real queue: no running one job inside another."""
    lane = InlineLane()
    ran: list[str] = []

    def outer() -> None:
        lane.submit(lambda: ran.append("inner"))
        ran.append("outer")

    lane.submit(outer)
    assert ran == ["outer", "inner"]


def test_held_work_runs_when_the_block_ends() -> None:
    lane = InlineLane()
    ran: list[str] = []
    with lane.held():
        lane.submit(lambda: ran.append("a"))
        lane.submit(lambda: ran.append("b"))
        assert ran == []
    assert ran == ["a", "b"]


def test_work_that_raises_reaches_the_caller() -> None:
    """So a test sees the crash, rather than a worker_error nobody listens to."""
    lane = InlineLane()

    def boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        lane.submit(boom)
    ran: list[str] = []
    lane.submit(lambda: ran.append("next"))
    assert ran == ["next"]


# -- the threaded session lane --------------------------------------------------


def test_queue_lane_runs_in_order_on_the_thread_that_runs_it() -> None:
    lane = QueueLane()
    ran: list[tuple[str, str]] = []
    for name in ("a", "b", "c"):
        lane.submit(lambda name=name: ran.append((name, threading.current_thread().name)))
    lane.stop()
    thread = threading.Thread(target=lane.run, args=(pytest.fail,), name="session")
    thread.start()
    thread.join(5)
    assert ran == [("a", "session"), ("b", "session"), ("c", "session")]


def test_queue_lane_reports_work_that_raises_and_carries_on() -> None:
    lane = QueueLane()
    errors: list[Exception] = []
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    lane.submit(boom)
    lane.submit(lambda: ran.append("after"))
    lane.stop()
    lane.run(errors.append)
    assert [str(e) for e in errors] == ["boom"]
    assert ran == ["after"]


def test_the_threaded_worker_runs_jobs_then_tears_down_on_shutdown(qapp) -> None:
    conn = FakeConnection()
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    thread = threading.Thread(target=worker.run_loop)
    thread.start()
    worker.submit(ConnectJob(params=ConnectParams(name="dev")))
    worker.submit(RunScriptJob(statements=split_sql("select 1")))
    worker.shutdown()
    thread.join(5)
    assert not thread.is_alive()
    assert "select 1" in conn.executed
    assert conn.closed  # the session closed on the worker's own thread
