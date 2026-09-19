"""Asynchronous statement execution and cancellation (Q3, Q4, spec 7.4)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from snowdesk.db.errors import is_cancellation
from snowdesk.db.session import Connection
from snowdesk.model import Statement

log = logging.getLogger(__name__)

INITIAL_POLL_SECONDS = 0.1
MAX_POLL_SECONDS = 1.0
POLL_BACKOFF = 1.5


class Cancelled(Exception):
    """Raised inside the worker when the running statement was cancelled."""


class StatementRunner:
    """Runs one statement at a time on the worker thread's connection.

    Statements go through ``execute_async`` so the connection is not blocked
    while Snowflake works; that is what lets a cancel run on a second cursor
    from another thread (spec 6.2).
    """

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._stop = threading.Event()
        self._current_qid: str | None = None
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------

    @property
    def current_query_id(self) -> str | None:
        with self._lock:
            return self._current_qid

    def reset(self) -> None:
        self._stop.clear()
        with self._lock:
            self._current_qid = None

    # -- execution ---------------------------------------------------------

    def run(self, statement: Statement, on_started: Callable[[str | None], None]) -> Any:
        """Execute ``statement`` and return a cursor positioned on its results.

        Raises :class:`Cancelled` if the statement was cancelled, or the
        connector's ``ProgrammingError`` on failure.
        """
        if statement.is_put_or_get:
            # PUT/GET cannot run through execute_async (spec 7.3).
            return self._run_sync(statement, on_started)
        return self._run_async(statement, on_started)

    def _run_sync(self, statement: Statement, on_started: Callable[[str | None], None]) -> Any:
        cur = self.conn.cursor()
        on_started(None)
        cur.execute(statement.sql)
        with self._lock:
            self._current_qid = getattr(cur, "sfqid", None)
        return cur

    def _run_async(self, statement: Statement, on_started: Callable[[str | None], None]) -> Any:
        cur = self.conn.cursor()
        cur.execute_async(statement.sql)
        qid = str(getattr(cur, "sfqid", "") or "")
        if not qid:
            # Without a query id there is nothing to poll or cancel; fall back
            # to the synchronous path rather than spinning forever.
            return self._run_sync(statement, on_started)
        with self._lock:
            self._current_qid = qid
        on_started(qid)

        delay = INITIAL_POLL_SECONDS
        while True:
            if self._stop.is_set():
                raise Cancelled
            try:
                status = self.conn.get_query_status_throw_if_error(qid)
            except Exception as exc:
                if is_cancellation(exc) or self._stop.is_set():
                    raise Cancelled from exc
                raise
            if not self.conn.is_still_running(status):
                break
            # Interruptible sleep: a cancel wakes the loop immediately.
            if self._stop.wait(delay):
                raise Cancelled
            delay = min(delay * POLL_BACKOFF, MAX_POLL_SECONDS)

        try:
            cur.get_results_from_sfqid(qid)
        except Exception as exc:
            if is_cancellation(exc):
                raise Cancelled from exc
            raise
        return cur

    # -- cancellation ------------------------------------------------------

    def request_stop(self) -> None:
        """Break the polling loop; safe to call from any thread."""
        self._stop.set()

    def cancel(self) -> str | None:
        """Cancel the running statement server-side (Q4).

        Called from the cancel thread, never from the worker thread, so it does
        not queue behind the statement it is cancelling.  Returns the query ID
        that was cancelled, if any.
        """
        qid = self.current_query_id
        self.request_stop()
        if not qid:
            return None
        try:
            cur = self.conn.cursor()
            try:
                cur.execute("SELECT SYSTEM$CANCEL_QUERY(%s)", (qid,))
            finally:
                cur.close()
        except Exception:
            log.warning("SYSTEM$CANCEL_QUERY failed for %s", qid, exc_info=True)
        return qid


def status_message(cursor: Any, elapsed: float) -> str:
    """Status line for DML/DDL statements that return no grid (Q6)."""
    rowcount = getattr(cursor, "rowcount", None)
    if isinstance(rowcount, int) and rowcount >= 0:
        noun = "row" if rowcount == 1 else "rows"
        return f"{rowcount:,} {noun} affected in {elapsed:.2f}s"
    return f"Statement executed in {elapsed:.2f}s"


def sleep_backoff(delay: float) -> float:
    """Pure helper so the backoff schedule can be unit-tested."""
    time.sleep(delay)
    return min(delay * POLL_BACKOFF, MAX_POLL_SECONDS)
