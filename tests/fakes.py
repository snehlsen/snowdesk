"""A fake Snowflake connection that simulates running, succeeded, failed and
cancelled statuses (spec 11)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class FakeProgrammingError(Exception):
    def __init__(
        self,
        msg: str,
        errno: int | None = None,
        sqlstate: str | None = None,
        sfqid: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.raw_msg = msg
        self.msg = msg
        self.errno = errno
        self.sqlstate = sqlstate
        self.sfqid = sfqid


@dataclass
class FakeStatement:
    """How the fake connection should behave for a statement."""

    columns: list[tuple] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: Exception | None = None
    #: Number of poll calls that report "still running" before finishing.
    polls: int = 0
    rowcount: int | None = None


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.sfqid: str | None = None
        self.description: list[tuple] | None = None
        self.rowcount: int = -1
        self._rows: list[tuple] = []
        self._pos = 0
        self.closed = False
        #: Set by get_results_from_sfqid and applied on the first fetch, the
        #: way the real connector's _prefetch_hook works: an async cursor has
        #: no description until rows are pulled from it.
        self._pending: FakeStatement | None = None

    # -- DB-API-ish --------------------------------------------------------

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.conn.executed.append(sql)
        if sql.upper().startswith("SELECT SYSTEM$CANCEL_QUERY"):
            self.conn.cancel_requested.set()
            qid = params[0] if params else None
            self.conn.cancelled_ids.append(qid)
            self.description = [("SYSTEM$CANCEL_QUERY", 2, None, None, None, None, True)]
            self._rows = [("cancelled",)]
            return self
        spec = self.conn.plan_for(sql)
        if spec.error is not None:
            raise spec.error
        self._apply(spec)
        return self

    def execute_async(self, sql: str) -> FakeCursor:
        self.conn.executed.append(sql)
        self.sfqid = self.conn.next_qid()
        self.conn.pending[self.sfqid] = (sql, self.conn.plan_for(sql).polls)
        return self

    def get_results_from_sfqid(self, sfqid: str) -> None:
        """Arm the result without materialising it, as the connector does."""
        sql, _ = self.conn.pending.get(sfqid, ("", 0))
        self._pending = self.conn.plan_for(sql)
        self.sfqid = sfqid

    def _prefetch(self) -> None:
        if self._pending is not None:
            spec, self._pending = self._pending, None
            self._apply(spec)

    def _apply(self, spec: FakeStatement) -> None:
        self.description = spec.columns or None
        self._rows = list(spec.rows)
        self._pos = 0
        self.rowcount = spec.rowcount if spec.rowcount is not None else len(spec.rows)

    def fetchmany(self, size: int) -> list[tuple]:
        self._prefetch()
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchall(self) -> list[tuple]:
        self._prefetch()
        chunk = self._rows[self._pos :]
        self._pos = len(self._rows)
        return chunk

    def fetchone(self) -> tuple | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Implements the ``Connection`` protocol from :mod:`snowdesk.db.session`."""

    def __init__(self, plan: dict[str, FakeStatement] | None = None) -> None:
        self.plan = plan or {}
        self.default = FakeStatement()
        self.executed: list[str] = []
        self.pending: dict[str, tuple[str, int]] = {}
        self.cancelled_ids: list[str | None] = []
        self.cancel_requested = threading.Event()
        self.closed = False
        self.role = "ANALYST"
        self.warehouse = "COMPUTE_WH"
        self.database = "RAW"
        self.schema = "PUBLIC"
        self._qid = 0
        self._polled: dict[str, int] = {}

    def next_qid(self) -> str:
        self._qid += 1
        return f"01b0-{self._qid:04d}"

    def plan_for(self, sql: str) -> FakeStatement:
        for key, spec in self.plan.items():
            if key.lower() in sql.lower():
                return spec
        return self.default

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    # -- async status ------------------------------------------------------

    def get_query_status_throw_if_error(self, sfqid: str) -> str:
        sql, polls = self.pending.get(sfqid, ("", 0))
        seen = self._polled.get(sfqid, 0)
        self._polled[sfqid] = seen + 1
        if self.cancel_requested.is_set():
            raise FakeProgrammingError("SQL execution canceled", errno=604, sfqid=sfqid)
        spec = self.plan_for(sql)
        if seen >= polls:
            if spec.error is not None:
                raise spec.error
            return "SUCCESS"
        return "RUNNING"

    def is_still_running(self, status: str) -> bool:
        return status == "RUNNING"
