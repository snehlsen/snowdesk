"""Turns editor intents into worker jobs and routes results back (Q1-Q6)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from snowdesk.config import DEFAULT_PAGE_SIZE, DEFAULT_ROW_CAP
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import CloseResultJob, FetchMoreJob, RunScriptJob, SnowflakeWorker
from snowdesk.model import RunStatus, Statement, StatementOutcome
from snowdesk.storage.history import HistoryStore

log = logging.getLogger(__name__)


def statement_at(text: str, position: int) -> Statement | None:
    """The statement under the cursor, for ⌘↩ (Q1).

    A cursor sitting on the boundary belongs to the statement that ends there,
    which is what you want after typing a trailing semicolon.
    """
    statements = split_sql(text)
    if not statements:
        return None
    for stmt in statements:
        if stmt.start <= position <= stmt.end:
            return stmt
    # Between statements: take the next one, else the last.
    for stmt in statements:
        if stmt.start > position:
            return stmt
    return statements[-1]


class QueryController(QObject):
    """Owns the run lifecycle for one editor."""

    #: A run was rejected before it reached the worker.
    rejected = Signal(str)
    run_started = Signal(object)  # list[Statement]
    run_finished = Signal(object)  # list[StatementOutcome]

    def __init__(
        self,
        worker: SnowflakeWorker,
        history: HistoryStore | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.history = history
        self.page_size = page_size
        self.row_cap = row_cap
        self._running = False
        self._connection_name = ""
        worker.statement_finished.connect(self._on_statement_finished)
        worker.script_finished.connect(self._on_script_finished)
        worker.connected.connect(self._on_connected)

    # -- state -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def _on_connected(self, name: str, _ctx: object) -> None:
        self._connection_name = name

    # -- intents -----------------------------------------------------------

    def run_text(self, text: str) -> list[Statement]:
        """Run every statement in ``text`` in order (⌘⇧↩, Q1/Q2)."""
        return self._run(split_sql(text))

    def run_selection(self, text: str, start: int, end: int) -> list[Statement]:
        """Run the selected text, or the statement under the cursor (⌘↩)."""
        if end > start:
            return self._run(split_sql(text[start:end]))
        stmt = statement_at(text, start)
        return self._run([stmt] if stmt else [])

    def _run(self, statements: list[Statement]) -> list[Statement]:
        if self._running:
            self.rejected.emit("A query is already running.")
            return []
        if not statements:
            self.rejected.emit("Nothing to run.")
            return []
        if not self.worker.session.is_connected:
            self.rejected.emit("Not connected.")
            return []
        self._running = True
        self.run_started.emit(statements)
        self.worker.submit(RunScriptJob(statements=statements, page_size=self.page_size))
        return statements

    def cancel(self) -> None:
        """Cancel the running statement server-side (⌘., Q4)."""
        self.worker.cancel_running()

    def fetch_more(self, result_id: str) -> None:
        self.worker.submit(FetchMoreJob(result_id=result_id, page_size=self.page_size))

    def close_result(self, result_id: str) -> None:
        self.worker.submit(CloseResultJob(result_id=result_id))

    # -- worker callbacks --------------------------------------------------

    def _on_statement_finished(self, outcome: StatementOutcome) -> None:
        if self.history is None or outcome.status is RunStatus.SKIPPED:
            return
        try:
            self.history.record(outcome, self._connection_name)
        except Exception:
            log.warning("Could not write history entry", exc_info=True)

    def _on_script_finished(self, outcomes: list[StatementOutcome]) -> None:
        self._running = False
        self.run_finished.emit(outcomes)
