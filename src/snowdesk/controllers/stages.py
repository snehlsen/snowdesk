"""Stage browser controller: listings, and one transfer at a time.

A transfer runs in two steps.  Planning (a ``LIST``, then working out names
and collisions) happens off the UI thread and comes back as
``plan_ready``; the UI asks about anything that would be overwritten and
hands the plan back through :meth:`confirm`.  Only one transfer, planned or
running, is in flight at once.
"""

from __future__ import annotations

import itertools
import logging

from PySide6.QtCore import QObject, Signal

from snowdesk.config import DEFAULT_ROW_CAP
from snowdesk.db import stages as stage_ops
from snowdesk.db.worker import ListStageJob, SnowflakeWorker, StagesJob
from snowdesk.model import (
    FileResult,
    StageFile,
    StageRef,
    StatementOutcome,
    TransferPlan,
    TransferProgress,
    TransferSummary,
)
from snowdesk.storage.history import HistoryStore

log = logging.getLogger(__name__)


class StageController(QObject):
    stages_ready = Signal(object)  # list[StageRef]
    stages_failed = Signal(str)
    listed = Signal(object, str, object, bool)  # stage, prefix, files, truncated
    list_failed = Signal(object, str, str)  # stage, prefix, message

    #: A plan is ready for the UI to confirm or abandon.
    plan_ready = Signal(object)  # TransferPlan
    transfer_started = Signal(object)  # TransferPlan
    progress = Signal(object)  # TransferProgress
    file_done = Signal(object)  # FileResult
    finished = Signal(object)  # TransferSummary
    #: The transfer never started: planning failed or was refused.
    failed = Signal(str)
    #: An intent was turned away before reaching the worker.
    rejected = Signal(str)

    def __init__(
        self,
        worker: SnowflakeWorker,
        history: HistoryStore | None = None,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.history = history
        self.row_cap = row_cap
        self._ids = itertools.count(1)
        #: The transfer being planned, confirmed or run, if any.
        self._active: str | None = None
        self._connection_name = ""
        worker.connected.connect(self._on_connected)
        worker.stages_ready.connect(self.stages_ready)
        worker.stages_failed.connect(self.stages_failed)
        worker.stage_listed.connect(self.listed)
        worker.stage_list_failed.connect(self.list_failed)
        worker.transfer_planned.connect(self._on_planned)
        worker.transfer_plan_failed.connect(self._on_plan_failed)
        worker.transfer_progress.connect(self._on_progress)
        worker.transfer_file_done.connect(self._on_file_done)
        worker.transfer_statement.connect(self._on_statement)
        worker.transfer_finished.connect(self._on_finished)

    @property
    def is_busy(self) -> bool:
        return self._active is not None

    def _on_connected(self, name: str, _ctx: object) -> None:
        self._connection_name = name

    # -- listing -----------------------------------------------------------

    def load_stages(self) -> None:
        self.worker.submit(StagesJob())

    def list_stage(self, stage: StageRef, prefix: str = "") -> None:
        self.worker.submit(ListStageJob(stage=stage, prefix=prefix, cap=self.row_cap))

    # -- transfers ---------------------------------------------------------

    def _begin(self) -> str | None:
        if self._active is not None:
            self.rejected.emit("A transfer is already running.")
            return None
        if not self.worker.session.is_connected:
            self.rejected.emit("Not connected.")
            return None
        self._active = f"t{next(self._ids)}"
        return self._active

    def upload(self, stage: StageRef, folder: str, paths: list[str]) -> None:
        if not paths:
            return
        if not stage.internal:
            self.rejected.emit("Files can only be uploaded to an internal stage.")
            return
        transfer_id = self._begin()
        if transfer_id is not None:
            self.worker.plan_upload(transfer_id, stage, folder, paths)

    def download(self, stage: StageRef, selection: list[str], local_root: str) -> None:
        if not selection or not local_root:
            return
        if not stage.internal:
            self.rejected.emit("Files can only be downloaded from an internal stage.")
            return
        transfer_id = self._begin()
        if transfer_id is not None:
            self.worker.plan_download(transfer_id, stage, selection, local_root)

    def remove(self, stage: StageRef, selection: list[StageFile]) -> None:
        """Delete files and folders; the UI has already confirmed (ST13)."""
        if not selection:
            return
        transfer_id = self._begin()
        if transfer_id is None:
            return
        plan = stage_ops.plan_remove(transfer_id, stage, selection)
        self._start(plan)

    def confirm(self, plan: TransferPlan, replace: bool) -> None:
        """Run a planned transfer, replacing its conflicts or leaving them."""
        if plan.transfer_id != self._active:
            return
        plan.replace = replace
        self._start(plan)

    def abandon(self, plan: TransferPlan) -> None:
        """The user backed out at the conflict prompt."""
        if plan.transfer_id == self._active:
            self._active = None

    def _start(self, plan: TransferPlan) -> None:
        self.transfer_started.emit(plan)
        self.worker.start_transfer(plan)

    def stop(self) -> None:
        if self._active is not None:
            self.worker.stop_transfer()

    # -- worker callbacks --------------------------------------------------

    def _on_planned(self, plan: TransferPlan) -> None:
        if plan.transfer_id == self._active:
            self.plan_ready.emit(plan)

    def _on_plan_failed(self, transfer_id: str, message: str) -> None:
        if transfer_id == self._active:
            self._active = None
            self.failed.emit(message)

    def _on_progress(self, progress: TransferProgress) -> None:
        if progress.transfer_id == self._active:
            self.progress.emit(progress)

    def _on_file_done(self, result: FileResult) -> None:
        if result.transfer_id == self._active:
            self.file_done.emit(result)

    def _on_statement(self, outcome: StatementOutcome) -> None:
        """Every PUT, GET and REMOVE goes into History like any statement (H1)."""
        if self.history is None:
            return
        try:
            self.history.record(outcome, self._connection_name)
        except Exception:
            log.warning("Could not write history entry", exc_info=True)

    def _on_finished(self, summary: TransferSummary) -> None:
        if summary.transfer_id == self._active:
            self._active = None
        self.finished.emit(summary)
