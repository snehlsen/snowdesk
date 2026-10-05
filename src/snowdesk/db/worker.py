"""The worker: jobs, Snowflake calls, Qt signals (spec 6.2).

All Snowflake I/O happens here.  Which thread runs it is the worker's
lanes' business (db/lanes.py): the session lane owns the connection, and
exports, transfers and cancels each run in a lane of their own.

Signals always carry plain Python data — never live cursors.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from PySide6.QtCore import QObject, Signal

from snowdesk.config import DEFAULT_PAGE_SIZE
from snowdesk.db import browser as browse
from snowdesk.db import export as csv_export
from snowdesk.db import profile
from snowdesk.db import stages as stage_ops
from snowdesk.db.errors import (
    is_bad_private_key_passphrase,
    is_mfa_refused,
    is_rejected_password,
    is_session_lost,
    needs_password,
    needs_private_key_passphrase,
    to_query_error,
)
from snowdesk.db.lanes import Lanes
from snowdesk.db.results import ResultHandle, ResultRegistry, summarize_status
from snowdesk.db.runner import Cancelled, StatementRunner
from snowdesk.db.session import Connection, ConnectParams, SnowflakeSession
from snowdesk.model import (
    ConnectFailure,
    ObjectNode,
    RunStatus,
    SessionContext,
    StageRef,
    Statement,
    StatementOutcome,
    TransactionState,
    TransferPlan,
    TransferSummary,
)

log = logging.getLogger(__name__)

#: A statement that may have changed the session's AUTOCOMMIT, so it is worth
#: asking the server again.  Loose on purpose: a false positive costs one SHOW.
_MENTIONS_AUTOCOMMIT = re.compile(r"\bautocommit\b", re.IGNORECASE)
#: Likewise for the current warehouse's size (``ALTER WAREHOUSE ... SET
#: WAREHOUSE_SIZE``), which the status bar shows.
_MENTIONS_WAREHOUSE = re.compile(r"\bwarehouse", re.IGNORECASE)


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
class ProfileJob:
    """Read the operator statistics for one query id (spec 7.4)."""

    query_id: str
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(slots=True)
class BrowseJob:
    #: Path into the tree: () = databases, (db,) = schemas,
    #: (db, schema) = objects, (db, schema, table) = columns.
    path: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class StagesJob:
    """Every stage the role can see, for the Stages sidebar (ST1)."""


@dataclass(slots=True)
class ListStageJob:
    """``LIST`` one stage, or one folder in it (ST2)."""

    stage: StageRef
    prefix: str = ""
    cap: int | None = None


@dataclass(slots=True)
class EndTransactionJob:
    """``COMMIT`` or ``ROLLBACK`` the open transaction (Q10)."""

    commit: bool


@dataclass(slots=True)
class SetAutocommitJob:
    """Switch the session's AUTOCOMMIT (Q10).  Refused mid-transaction."""

    enabled: bool


Job = (
    ConnectJob
    | DisconnectJob
    | RunScriptJob
    | FetchMoreJob
    | CloseResultJob
    | ProfileJob
    | BrowseJob
    | StagesJob
    | ListStageJob
    | EndTransactionJob
    | SetAutocommitJob
)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

LaneName = Literal["session", "export", "transfer"]


class Refused(Exception):
    """A request turned down before Snowflake is asked; its message is the answer."""


@dataclass(frozen=True, slots=True)
class Operation[T]:
    """One request to Snowflake: the lane it runs in, and where its answer goes.

    :meth:`SnowflakeWorker._run` puts it in the envelope every request
    shares: the connected check, the lost-session check, and turning a
    failure into the message ``failed`` hears.
    """

    name: str  # for the log: "<name> failed"
    work: Callable[[Connection], T]
    done: Callable[[T], None]
    failed: Callable[[str], None]
    lane: LaneName = "session"
    #: Failures whose own message is the answer, besides :class:`Refused`.
    refusals: tuple[type[Exception], ...] = ()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class SnowflakeWorker(QObject):
    """Runs Snowflake work in its lanes. Public methods are thread-safe."""

    # What happened to the session.  What it means for the Session lifecycle
    # is decided on the UI thread (controllers/session_lifecycle.py).
    #: Connection name, SessionContext, and whether the browser had to open
    #: again for SSO (a hint that ALLOW_ID_TOKEN is off).
    connected = Signal(str, object, bool)
    connect_failed = Signal(str, object, object)  # connection name, ConnectFailure, QueryError
    #: The session died mid-flight: connection name and what went wrong.
    session_lost = Signal(str, str)
    context_changed = Signal(object)  # SessionContext

    statement_started = Signal(int, object, str)  # index, Statement, query id
    statement_finished = Signal(object)  # StatementOutcome
    script_finished = Signal(object)  # list[StatementOutcome]

    # id, cols, rows, done, total, query id, tab label ('' = number it)
    result_ready = Signal(str, object, object, bool, object, str, str)
    rows_appended = Signal(str, object, bool)  # id, rows, exhausted
    fetch_failed = Signal(str, str)  # result id, message

    #: The profiled query ran but Snowflake kept no operator stats for it.
    profile_empty = Signal(str)  # query id
    profile_failed = Signal(str, str)  # query id, message

    export_progress = Signal(str, int)  # result id, rows written
    export_finished = Signal(str, str, int)  # result id, path, rows
    export_failed = Signal(str, str)  # result id, message
    export_cancelled = Signal(str)

    #: Commit mode or open transaction, re-read after anything that could
    #: have changed either.
    transaction_changed = Signal(object)  # TransactionState
    #: A Commit or Roll back from the status bar finished, either way.
    transaction_ended = Signal(object)  # StatementOutcome
    autocommit_failed = Signal(str)  # message

    nodes_ready = Signal(object, object)  # path tuple, list[ObjectNode]
    browse_failed = Signal(object, str)  # path tuple, message

    stages_ready = Signal(object)  # list[StageRef]
    stages_failed = Signal(str)
    stage_listed = Signal(object, str, object, bool)  # stage, prefix, files, truncated
    stage_list_failed = Signal(object, str, str)  # stage, prefix, message

    #: A transfer has been worked out and is waiting to be confirmed.
    transfer_planned = Signal(object)  # TransferPlan
    transfer_plan_failed = Signal(str, str)  # transfer id, message
    transfer_progress = Signal(object)  # TransferProgress
    transfer_file_done = Signal(object)  # FileResult
    #: Each PUT, GET or REMOVE a transfer ran, for Messages and History.
    transfer_statement = Signal(object)  # StatementOutcome
    transfer_finished = Signal(object)  # TransferSummary

    worker_error = Signal(str)

    def __init__(self, session: SnowflakeSession | None = None, lanes: Lanes | None = None) -> None:
        super().__init__()
        self.session = session or SnowflakeSession()
        self.results = ResultRegistry()
        # Exports and stage transfers run off the session lane: a million
        # rows, or a PUT, takes minutes, and blocking every other job behind
        # it would freeze the browser and any further queries.  Each uses its
        # own cursor, as the cancel lane already does (spec 6.2,
        # docs/stage-browser.md §7.2).
        self._lanes = lanes or Lanes.threaded()
        self._runner: StatementRunner | None = None
        self._runner_lock = threading.Lock()
        self._export_stop = threading.Event()
        self._transfer_stop = threading.Event()
        self._transaction = TransactionState()

    # -- public API (called from the UI thread) ---------------------------

    def submit(self, job: Job) -> None:
        self._lanes.session.submit(lambda: self._dispatch(job))

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
        self._lanes.cancel.submit(lambda: self._do_cancel(runner))

    def _do_cancel(self, runner: StatementRunner) -> None:
        try:
            runner.cancel()
        except Exception:
            log.warning("Cancel failed", exc_info=True)

    # -- export (R6) -------------------------------------------------------

    def export_csv(
        self, result_id: str, path: str, page_size: int, escape_formulas: bool = True
    ) -> None:
        """Stream a finished result to a CSV file, in the export lane."""
        handle = self.results.get(result_id)
        if handle is None or not handle.query_id:
            self.export_failed.emit(
                result_id, "That result can no longer be exported; run the query again."
            )
            return
        query_id = handle.query_id
        self._export_stop.clear()

        def export(conn: Connection) -> int | None:
            """Rows written, or None when cancelled; no partial file is left either way."""
            try:
                return csv_export.export_result(
                    conn,
                    query_id,
                    path,
                    page_size,
                    self._export_stop,
                    on_progress=lambda written: self.export_progress.emit(result_id, written),
                    escape_formulas=escape_formulas,
                )
            except csv_export.ExportCancelled:
                csv_export.discard(path)
                return None
            except Exception:
                csv_export.discard(path)
                raise

        def finished(rows: int | None) -> None:
            if rows is None:
                self.export_cancelled.emit(result_id)
            else:
                self.export_finished.emit(result_id, path, rows)

        self._submit(
            Operation(
                f"Export of {result_id}",
                work=export,
                done=finished,
                failed=lambda message: self.export_failed.emit(result_id, message),
                lane="export",
            )
        )

    def cancel_export(self) -> None:
        self._export_stop.set()

    # -- stage transfers (docs/stage-browser.md) ---------------------------

    def plan_upload(self, transfer_id: str, stage: StageRef, folder: str, paths: list[str]) -> None:
        """Work out an upload off the UI thread; answers with ``transfer_planned``."""
        self._submit_plan(transfer_id, stage_ops.plan_upload, stage, folder, paths)

    def plan_download(
        self, transfer_id: str, stage: StageRef, selection: list[str], local_root: str
    ) -> None:
        self._submit_plan(transfer_id, stage_ops.plan_download, stage, selection, local_root)

    def _submit_plan(self, transfer_id: str, planner: Any, *args: Any) -> None:
        self._submit(
            Operation(
                f"Planning transfer {transfer_id}",
                work=lambda conn: planner(conn, transfer_id, *args),
                done=self.transfer_planned.emit,
                failed=lambda message: self.transfer_plan_failed.emit(transfer_id, message),
                lane="transfer",
                refusals=(stage_ops.UnsafeName,),
            )
        )

    def start_transfer(self, plan: TransferPlan) -> None:
        """Run a confirmed plan; Stop takes effect between statements (ST8)."""
        if not self.session.is_connected:
            self.transfer_plan_failed.emit(plan.transfer_id, "Not connected.")
            return
        self._transfer_stop.clear()
        self._lanes.transfer.submit(lambda: self._do_transfer(plan))

    def stop_transfer(self) -> None:
        self._transfer_stop.set()

    def _do_transfer(self, plan: TransferPlan) -> None:
        callbacks = stage_ops.Callbacks(
            progress=self.transfer_progress.emit,
            file_done=self.transfer_file_done.emit,
            statement=self.transfer_statement.emit,
        )
        try:
            summary = stage_ops.run_transfer(
                self.session.connection, plan, self._transfer_stop, callbacks
            )
        except Exception as exc:
            # run_transfer handles what it expects; anything else is a bug,
            # and the UI must still hear that the transfer is over.
            log.exception("Transfer %s failed", plan.transfer_id)
            summary = TransferSummary(
                transfer_id=plan.transfer_id,
                kind=plan.kind,
                stage=plan.stage,
                error=f"{type(exc).__name__}: {exc}",
            )
        if summary.session_lost:
            self.session_lost.emit(self.session.connection_name or "", summary.error)
        self.transfer_finished.emit(summary)

    def shutdown(self) -> None:
        self._export_stop.set()
        self._transfer_stop.set()
        self._lanes.session.stop()

    # -- session lane (runs on the worker thread) -------------------------

    def run_loop(self) -> None:
        log.info("Worker thread started")
        self._lanes.session.run(on_error=self._job_crashed)
        self._teardown()
        log.info("Worker thread stopped")

    def _job_crashed(self, exc: Exception) -> None:
        log.error("Unhandled error in worker job", exc_info=exc)
        self.worker_error.emit(f"{type(exc).__name__}: {exc}")

    def _teardown(self) -> None:
        self.results.close_all()
        self.session.close()
        self._lanes.cancel.stop()
        self._lanes.export.stop()
        self._lanes.transfer.stop()

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
        elif isinstance(job, ProfileJob):
            self._profile(job)
        elif isinstance(job, BrowseJob):
            self._browse(job)
        elif isinstance(job, StagesJob):
            self._list_stages()
        elif isinstance(job, ListStageJob):
            self._list_stage(job)
        elif isinstance(job, EndTransactionJob):
            self._end_transaction(job)
        elif isinstance(job, SetAutocommitJob):
            self._set_autocommit(job)

    # -- connection -------------------------------------------------------

    def _connect(self, job: ConnectJob) -> None:
        self.results.close_all()
        params = job.params
        try:
            ctx = self.session.connect(params)
        except Exception as exc:
            kind = _connect_failure(exc, params)
            self.connect_failed.emit(params.name, kind, to_query_error(exc))
            log.info("Connect to %s failed: %s", params.name, exc)
            return
        self.connected.emit(params.name, ctx, self.session.should_hint_id_token())
        self.context_changed.emit(ctx)
        self._refresh_transaction(reread_autocommit=True)

    def _disconnect(self) -> None:
        """Close the session; also how a lost one is cleaned up."""
        self.results.close_all()
        self.session.close()
        self.context_changed.emit(SessionContext())
        self._set_transaction(TransactionState())

    # -- the envelope every operation runs in ---------------------------

    def _submit(self, op: Operation[Any]) -> None:
        """Run ``op`` in its lane.  For the lanes other than the session's."""
        lane = self._lanes.export if op.lane == "export" else self._lanes.transfer
        lane.submit(lambda: self._run(op))

    def _run(self, op: Operation[Any]) -> None:
        """Run ``op`` here and now, on the lane it names."""
        if not self.session.is_connected:
            op.failed("Not connected.")
            return
        refusals: tuple[type[Exception], ...] = (Refused, *op.refusals)
        try:
            answer = op.work(self.session.connection)
        except refusals as exc:
            op.failed(str(exc))
            return
        except Exception as exc:
            log.warning("%s failed", op.name, exc_info=True)
            self._note_failure(exc, op.lane)
            op.failed(to_query_error(exc).formatted())
            return
        op.done(answer)

    def _note_failure(self, exc: BaseException, lane: LaneName = "session") -> bool:
        """Report the session as lost if ``exc`` says the connection is gone (spec 9).

        Returns whether it did.  On the session lane the session is closed
        here, on the thread that owns it, so the rest of the job fails fast
        instead of waiting on a dead socket.  The other lanes only report:
        the session is not theirs to close, so the Session lifecycle queues
        the closing on the session lane.  Either way it decides what the
        loss means.
        """
        if not is_session_lost(exc):
            return False
        name = self.session.connection_name or ""
        message = to_query_error(exc).message
        log.warning("Connection %s lost: %s", name, message)
        if lane == "session":
            self.results.close_all()
            self.session.close()
        self.session_lost.emit(name, message)
        return True

    # -- script execution -------------------------------------------------

    def _run_script(self, job: RunScriptJob) -> None:
        if not self.session.is_connected:
            self.worker_error.emit("Not connected.")
            self.script_finished.emit([])
            return

        runner = StatementRunner(self.session.connection)
        with self._runner_lock:
            self._runner = runner
        outcomes: list[StatementOutcome] = []
        try:
            self._execute_statements(job, runner, outcomes)
        finally:
            with self._runner_lock:
                self._runner = None
            self.context_changed.emit(
                self.session.read_context(
                    recheck_warehouse=any(_MENTIONS_WAREHOUSE.search(s.sql) for s in job.statements)
                )
            )
            self._refresh_transaction(
                reread_autocommit=any(_MENTIONS_AUTOCOMMIT.search(s.sql) for s in job.statements)
            )
            self.script_finished.emit(outcomes)

    def _execute_statements(
        self, job: RunScriptJob, runner: StatementRunner, outcomes: list[StatementOutcome]
    ) -> None:
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
            self._note_failure(exc)
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
            if not self._note_failure(exc):
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
            handle.result_id, handle.columns, rows, handle.exhausted, handle.total, qid or "", ""
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

    # -- transactions (Q10) -----------------------------------------------

    def _set_transaction(self, state: TransactionState) -> None:
        self._transaction = state
        self.transaction_changed.emit(state)

    def _refresh_transaction(self, *, reread_autocommit: bool) -> None:
        """Ask the session for its commit mode and open transaction.

        ``CURRENT_TRANSACTION()`` is read every time: it is one round trip
        with no warehouse, and parsing for BEGIN and COMMIT would miss
        implicit commits by DDL and whatever a procedure does.  AUTOCOMMIT
        only changes when something sets it, so it is re-read only then.
        """
        if not self.session.is_connected:
            self._set_transaction(TransactionState())
            return
        autocommit = (
            self.session.read_autocommit() if reread_autocommit else self._transaction.autocommit
        )
        try:
            transaction_id = self.session.read_transaction_id()
        except Exception as exc:
            if self._note_failure(exc):
                return
            log.debug("Could not read the open transaction", exc_info=True)
            transaction_id = self._transaction.transaction_id
        self._set_transaction(TransactionState(autocommit, transaction_id))

    def _end_transaction(self, job: EndTransactionJob) -> None:
        """Run COMMIT or ROLLBACK and report it like any other statement.

        Not a :class:`RunScriptJob`: a run clears the result tabs, and the
        grid the user checked before committing should still be there after.
        """
        sql = "COMMIT" if job.commit else "ROLLBACK"
        statement = Statement(sql=sql, start=0, end=0)
        if not self.session.is_connected:
            self.transaction_ended.emit(
                StatementOutcome(0, statement, RunStatus.ERROR, message="Not connected.")
            )
            return
        started = time.monotonic()
        cursor = None
        try:
            cursor = self.session.connection.cursor()
            cursor.execute(sql)
        except Exception as exc:
            self._note_failure(exc)
            error = to_query_error(exc, getattr(cursor, "sfqid", None))
            outcome = StatementOutcome(
                0,
                statement,
                RunStatus.ERROR,
                query_id=error.query_id,
                duration_s=time.monotonic() - started,
                message=error.formatted(),
                error=error,
            )
        else:
            elapsed = time.monotonic() - started
            outcome = StatementOutcome(
                0,
                statement,
                RunStatus.SUCCESS,
                query_id=getattr(cursor, "sfqid", None),
                duration_s=elapsed,
                message=f"{'Committed' if job.commit else 'Rolled back'} ({elapsed:.2f}s)",
            )
        finally:
            if cursor is not None:
                cursor.close()
        self._refresh_transaction(reread_autocommit=False)
        self.transaction_ended.emit(outcome)

    def _set_autocommit(self, job: SetAutocommitJob) -> None:
        if not self.session.is_connected:
            self.autocommit_failed.emit("Not connected.")
            return
        # Checked here rather than trusted from the UI: the transaction may
        # have been opened by a statement queued ahead of this job, and a
        # COMMIT queued ahead of it may have failed.
        self._refresh_transaction(reread_autocommit=False)
        if not self.session.is_connected:
            return
        if self._transaction.in_transaction:
            self.autocommit_failed.emit(
                "A transaction is open. Commit or roll it back before changing the commit mode."
            )
            return
        try:
            self.session.set_autocommit(job.enabled)
        except Exception as exc:
            if not self._note_failure(exc):
                log.warning("Could not set AUTOCOMMIT", exc_info=True)
                self.autocommit_failed.emit(to_query_error(exc).formatted())
                self._refresh_transaction(reread_autocommit=True)
            return
        self._refresh_transaction(reread_autocommit=True)

    # -- incremental fetching ---------------------------------------------

    def _fetch_more(self, job: FetchMoreJob) -> None:
        handle = self.results.get(job.result_id)
        if handle is None or handle.closed:
            self.rows_appended.emit(job.result_id, [], True)
            return

        def failed(message: str) -> None:
            self.results.close(job.result_id)
            self.fetch_failed.emit(job.result_id, message)

        self._run(
            Operation(
                f"Fetch for {job.result_id}",
                work=lambda _conn: handle.fetch(job.page_size),
                done=lambda rows: self.rows_appended.emit(job.result_id, rows, handle.exhausted),
                failed=failed,
            )
        )

    # -- query profile ----------------------------------------------------

    def _profile(self, job: ProfileJob) -> None:
        """Open the operator statistics for ``job.query_id`` as a result tab.

        Run synchronously, like the browser's ``SHOW``: a profile is a few
        dozen rows and putting it through the async path would take over the
        runner that the statement being profiled may still be using.
        """
        qid = job.query_id.strip()

        def read(conn: Connection) -> tuple[ResultHandle, list[Any]]:
            if not profile.is_query_id(qid):
                raise Refused(f"{qid or '(empty)'} is not a Snowflake query id.")
            cursor = conn.cursor()
            cursor.execute(profile.profile_sql(qid))
            handle = self.results.register(cursor)
            try:
                return handle, handle.fetch(job.page_size)
            except Exception:
                self.results.close(handle.result_id)
                raise

        def show(answer: tuple[ResultHandle, list[Any]]) -> None:
            handle, rows = answer
            if not rows:
                self.results.close(handle.result_id)
                self.profile_empty.emit(qid)
                return
            # The tab carries the *profiled* query's id, not the id of the
            # GET_QUERY_OPERATOR_STATS call that filled it: the profiled
            # query is the one worth copying out of here.
            self.result_ready.emit(
                handle.result_id,
                handle.columns,
                rows,
                handle.exhausted,
                handle.total,
                qid,
                f"Profile · {profile.short_id(qid)}",
            )

        self._run(
            Operation(
                f"Profile of {qid}",
                work=read,
                done=show,
                failed=lambda message: self.profile_failed.emit(qid, message),
            )
        )

    # -- object browser ---------------------------------------------------

    def _browse(self, job: BrowseJob) -> None:
        path = job.path
        self._run(
            Operation(
                "Browsing",
                work=lambda conn: _list_children(conn, path),
                done=lambda nodes: self.nodes_ready.emit(path, nodes),
                failed=lambda message: self.browse_failed.emit(path, message),
            )
        )

    # -- stages (docs/stage-browser.md) ------------------------------------

    def _list_stages(self) -> None:
        self._run(
            Operation(
                "Listing stages",
                work=stage_ops.list_stages,
                done=self.stages_ready.emit,
                failed=self.stages_failed.emit,
            )
        )

    def _list_stage(self, job: ListStageJob) -> None:
        stage, prefix = job.stage, job.prefix
        self._run(
            Operation(
                f"Listing {stage.name}",
                work=lambda conn: stage_ops.list_files(conn, stage, prefix, job.cap),
                done=lambda found: self.stage_listed.emit(stage, prefix, *found),
                failed=lambda message: self.stage_list_failed.emit(stage, prefix, message),
                refusals=(stage_ops.UnsafeName,),
            )
        )


def _list_children(conn: Connection, path: tuple[str, ...]) -> list[ObjectNode]:
    """The object browser's children of ``path``: databases down to columns."""
    if len(path) == 0:
        return browse.list_databases(conn)
    if len(path) == 1:
        return browse.list_schemas(conn, path[0])
    if len(path) == 2:
        return browse.list_objects(conn, path[0], path[1])
    return browse.list_columns(conn, path[0], path[1], path[2])


def _connect_failure(exc: BaseException, params: ConnectParams) -> ConnectFailure:
    if is_bad_private_key_passphrase(exc):
        return ConnectFailure.PASSPHRASE_REJECTED
    if needs_private_key_passphrase(exc):
        return ConnectFailure.PASSPHRASE_NEEDED
    if needs_password(exc):
        return ConnectFailure.PASSWORD_NEEDED
    # Only a password SnowDesk asked for counts as rejected: a wrong one in
    # connections.toml is the file's to fix, and prompting over it would hide
    # that the file is wrong.
    if params.password and is_rejected_password(exc):
        return ConnectFailure.PASSWORD_REJECTED
    # The password came from the file and got past Snowflake; only the MFA
    # step is missing, and a passcode is the one thing the file cannot hold.
    if is_mfa_refused(exc):
        return ConnectFailure.PASSCODE_NEEDED
    return ConnectFailure.ERROR


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
