"""The worker thread: job queue, Snowflake calls, Qt signals (spec 6.2).

All Snowflake I/O happens here, on one dedicated thread that owns the
connection.  Cancellation is the single exception: it runs on a small
side-thread so it does not queue behind the statement it is cancelling.

Signals always carry plain Python data — never live cursors.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from PySide6.QtCore import QObject, Signal

from snowdesk.config import DEFAULT_PAGE_SIZE
from snowdesk.db import browser as browse
from snowdesk.db.errors import (
    is_bad_private_key_passphrase,
    needs_private_key_passphrase,
    to_query_error,
)
from snowdesk.db.results import ResultRegistry, summarize_status
from snowdesk.db.runner import Cancelled, StatementRunner
from snowdesk.db.session import ConnectionState, ConnectParams, SnowflakeSession
from snowdesk.model import QueryError, RunStatus, SessionContext, Statement, StatementOutcome

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectJob:
    params: ConnectParams


@dataclass(slots=True)
class DisconnectJob:
    pass


@dataclass(slots=True)
class RunScriptJob:
    statements: list[Statement]
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(slots=True)
class FetchMoreJob:
    result_id: str
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(slots=True)
class CloseResultJob:
    result_id: str


@dataclass(slots=True)
class BrowseJob:
    #: Path into the tree: () = databases, (db,) = schemas,
    #: (db, schema) = objects, (db, schema, table) = columns.
    path: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ShutdownJob:
    pass


Job = (
    ConnectJob
    | DisconnectJob
    | RunScriptJob
    | FetchMoreJob
    | CloseResultJob
    | BrowseJob
    | ShutdownJob
)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class SnowflakeWorker(QObject):
    """Runs the job loop on its own thread. Public methods are thread-safe."""

    state_changed = Signal(str, str)  # ConnectionState value, detail
    context_changed = Signal(object)  # SessionContext
    connected = Signal(str, object)  # connection name, SessionContext
    connect_failed = Signal(object)  # QueryError
    #: The connection uses an encrypted private key and needs a passphrase:
    #: connection name, and whether a previous attempt was rejected.
    passphrase_required = Signal(str, bool)
    sso_hint = Signal()

    script_started = Signal(int)  # statement count
    statement_started = Signal(int, object, str)  # index, Statement, query id
    statement_finished = Signal(object)  # StatementOutcome
    script_finished = Signal(object)  # list[StatementOutcome]

    result_ready = Signal(str, object, object, bool, object)  # id, cols, rows, done, total
    rows_appended = Signal(str, object, bool)  # id, rows, exhausted
    fetch_failed = Signal(str, str)  # result id, message

    nodes_ready = Signal(object, object)  # path tuple, list[ObjectNode]
    browse_failed = Signal(object, str)  # path tuple, message

    worker_error = Signal(str)

    def __init__(self, session: SnowflakeSession | None = None) -> None:
        super().__init__()
        self.session = session or SnowflakeSession()
        self.results = ResultRegistry()
        self._queue: queue.Queue[Job] = queue.Queue()
        self._runner: StatementRunner | None = None
        self._runner_lock = threading.Lock()
        self._cancel_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="snowdesk-cancel")
        self._busy = threading.Event()
        # Key passphrases the user has entered this run, so Disconnect followed
        # by Connect does not ask again.  In memory only, never persisted, and
        # dropped as soon as one is rejected.
        self._passphrases: dict[str, str] = {}

    # -- public API (called from the UI thread) ---------------------------

    def submit(self, job: Job) -> None:
        self._queue.put(job)

    @property
    def is_busy(self) -> bool:
        return self._busy.is_set()

    def cancel_running(self) -> None:
        """Cancel the running statement server-side (Q4).

        Returns immediately; the actual ``SYSTEM$CANCEL_QUERY`` runs on the
        cancel thread so it never waits behind the running statement.
        """
        with self._runner_lock:
            runner = self._runner
        if runner is None:
            return
        runner.request_stop()
        self._cancel_pool.submit(self._do_cancel, runner)

    def _do_cancel(self, runner: StatementRunner) -> None:
        try:
            runner.cancel()
        except Exception:
            log.warning("Cancel failed", exc_info=True)

    def shutdown(self) -> None:
        self._queue.put(ShutdownJob())

    # -- job loop (runs on the worker thread) -----------------------------

    def run_loop(self) -> None:
        log.info("Worker thread started")
        while True:
            job = self._queue.get()
            if isinstance(job, ShutdownJob):
                break
            try:
                self._dispatch(job)
            except Exception as exc:
                log.exception("Unhandled error in worker job %s", type(job).__name__)
                self.worker_error.emit(f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()
        self._teardown()
        log.info("Worker thread stopped")

    def _teardown(self) -> None:
        self.results.close_all()
        self.session.close()
        self._cancel_pool.shutdown(wait=False)
        self.state_changed.emit(ConnectionState.DISCONNECTED.value, "")

    def _dispatch(self, job: Job) -> None:
        if isinstance(job, ConnectJob):
            self._connect(job)
        elif isinstance(job, DisconnectJob):
            self._disconnect()
        elif isinstance(job, RunScriptJob):
            self._run_script(job)
        elif isinstance(job, FetchMoreJob):
            self._fetch_more(job)
        elif isinstance(job, CloseResultJob):
            self.results.close(job.result_id)
        elif isinstance(job, BrowseJob):
            self._browse(job)

    # -- connection -------------------------------------------------------

    def _connect(self, job: ConnectJob) -> None:
        self.results.close_all()
        params = job.params
        if params.private_key_passphrase is None:
            remembered = self._passphrases.get(params.name)
            if remembered is not None:
                params = replace(params, private_key_passphrase=remembered)
        self.state_changed.emit(ConnectionState.CONNECTING.value, params.name)
        try:
            ctx = self.session.connect(params)
        except Exception as exc:
            self._on_connect_error(params, exc)
            return
        if params.private_key_passphrase:
            self._passphrases[params.name] = params.private_key_passphrase
        self.state_changed.emit(ConnectionState.CONNECTED.value, params.name)
        self.connected.emit(params.name, ctx)
        self.context_changed.emit(ctx)
        if self.session.should_hint_id_token():
            self.sso_hint.emit()

    def _on_connect_error(self, params: ConnectParams, exc: BaseException) -> None:
        """Route an encrypted-key failure to the passphrase prompt, not an error.

        The key is encrypted and either no passphrase was given or the one we
        had is wrong; in both cases the user can still get connected, so this
        is a prompt rather than a dead end.
        """
        rejected = is_bad_private_key_passphrase(exc)
        if needs_private_key_passphrase(exc) or rejected:
            if rejected:
                self._passphrases.pop(params.name, None)
            log.info("Connection %s needs a private key passphrase", params.name)
            self.state_changed.emit(ConnectionState.DISCONNECTED.value, "")
            self.passphrase_required.emit(params.name, rejected)
            return
        error = to_query_error(exc)
        log.warning("Connect to %s failed: %s", params.name, error.message)
        self.state_changed.emit(ConnectionState.ERROR.value, error.message)
        self.connect_failed.emit(error)

    def _disconnect(self) -> None:
        self.results.close_all()
        self.session.close()
        self.state_changed.emit(ConnectionState.DISCONNECTED.value, "")
        self.context_changed.emit(SessionContext())

    # -- script execution -------------------------------------------------

    def _run_script(self, job: RunScriptJob) -> None:
        if not self.session.is_connected:
            self.worker_error.emit("Not connected.")
            self.script_finished.emit([])
            return

        runner = StatementRunner(self.session.connection)
        with self._runner_lock:
            self._runner = runner
        self._busy.set()
        outcomes: list[StatementOutcome] = []
        try:
            self._execute_statements(job, runner, outcomes)
        finally:
            self._busy.clear()
            with self._runner_lock:
                self._runner = None
            self.context_changed.emit(self.session.read_context())
            self.script_finished.emit(outcomes)

    def _execute_statements(
        self, job: RunScriptJob, runner: StatementRunner, outcomes: list[StatementOutcome]
    ) -> None:
        self.script_started.emit(len(job.statements))
        stopped = False
        for index, statement in enumerate(job.statements):
            if stopped:
                outcome = StatementOutcome(
                    index=index,
                    statement=statement,
                    status=RunStatus.SKIPPED,
                    message="Skipped after an earlier statement failed.",
                )
                outcomes.append(outcome)
                self.statement_finished.emit(outcome)
                continue
            outcome = self._execute_one(index, statement, runner, job.page_size)
            outcomes.append(outcome)
            self.statement_finished.emit(outcome)
            if outcome.status in (RunStatus.ERROR, RunStatus.CANCELLED):
                stopped = True  # Q2: stop on first error

    def _execute_one(
        self, index: int, statement: Statement, runner: StatementRunner, page_size: int
    ) -> StatementOutcome:
        runner.reset()
        started = time.monotonic()

        def on_started(qid: str | None) -> None:
            self.statement_started.emit(index, statement, qid or "")

        try:
            cursor = runner.run(statement, on_started)
        except Cancelled:
            return StatementOutcome(
                index=index,
                statement=statement,
                status=RunStatus.CANCELLED,
                query_id=runner.current_query_id,
                duration_s=time.monotonic() - started,
                message="Cancelled.",
            )
        except Exception as exc:
            error = to_query_error(exc, runner.current_query_id)
            return StatementOutcome(
                index=index,
                statement=statement,
                status=RunStatus.ERROR,
                query_id=error.query_id,
                duration_s=time.monotonic() - started,
                message=error.formatted(),
                error=error,
            )

        elapsed = time.monotonic() - started
        qid = getattr(cursor, "sfqid", None) or runner.current_query_id

        # The first fetch is what makes an async cursor hand over its column
        # metadata, so it has to happen before the result can be classified.
        handle = self.results.register(cursor)
        try:
            rows = handle.fetch(page_size)
        except Exception as exc:
            self.results.close(handle.result_id)
            error = to_query_error(exc, qid)
            return StatementOutcome(
                index=index,
                statement=statement,
                status=RunStatus.ERROR,
                query_id=qid,
                duration_s=time.monotonic() - started,
                message=error.formatted(),
                error=error,
            )

        summary = summarize_status(handle.columns, rows, statement.sql)
        if summary is not None:
            self.results.close(handle.result_id)
            return StatementOutcome(
                index=index,
                statement=statement,
                status=RunStatus.SUCCESS,
                query_id=qid,
                duration_s=elapsed,
                row_count=_affected_rows(handle.columns, rows),
                message=f"{summary} ({elapsed:.2f}s)",
            )

        self.result_ready.emit(
            handle.result_id, handle.columns, rows, handle.exhausted, handle.total
        )
        return StatementOutcome(
            index=index,
            statement=statement,
            status=RunStatus.SUCCESS,
            query_id=qid,
            duration_s=elapsed,
            row_count=handle.total if handle.total is not None else handle.loaded,
            message=f"Success in {elapsed:.2f}s",
            result_id=handle.result_id,
            columns=list(handle.columns),
        )

    # -- incremental fetching ---------------------------------------------

    def _fetch_more(self, job: FetchMoreJob) -> None:
        handle = self.results.get(job.result_id)
        if handle is None or handle.closed:
            self.rows_appended.emit(job.result_id, [], True)
            return
        try:
            rows = handle.fetch(job.page_size)
        except Exception as exc:
            log.warning("Fetch failed for %s", job.result_id, exc_info=True)
            self.results.close(job.result_id)
            self.fetch_failed.emit(job.result_id, to_query_error(exc).formatted())
            return
        self.rows_appended.emit(job.result_id, rows, handle.exhausted)

    # -- object browser ---------------------------------------------------

    def _browse(self, job: BrowseJob) -> None:
        if not self.session.is_connected:
            self.browse_failed.emit(job.path, "Not connected.")
            return
        conn = self.session.connection
        path = job.path
        try:
            if len(path) == 0:
                nodes = browse.list_databases(conn)
            elif len(path) == 1:
                nodes = browse.list_schemas(conn, path[0])
            elif len(path) == 2:
                nodes = browse.list_objects(conn, path[0], path[1])
            else:
                nodes = browse.list_columns(conn, path[0], path[1], path[2])
        except Exception as exc:
            error: QueryError = to_query_error(exc)
            self.browse_failed.emit(path, error.message)
            return
        self.nodes_ready.emit(path, nodes)


def _affected_rows(columns: list[Any], rows: list[Any]) -> int | None:
    """Rows affected, when Snowflake reported them as a counter column (Q6)."""
    if len(rows) != 1 or not columns:
        return None
    total = 0
    for column, value in zip(columns, rows[0], strict=False):
        if not column.name.strip().lower().startswith("number of rows"):
            return None
        try:
            total += int(value)
        except (TypeError, ValueError):
            return None
    return total
