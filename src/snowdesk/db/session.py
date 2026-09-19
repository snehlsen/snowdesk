"""The Snowflake connection (C3-C6).

Only this package imports ``snowflake.connector``.  A :class:`SnowflakeSession`
is owned by the worker thread and must not be touched from the UI thread.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from snowdesk.model import SessionContext

log = logging.getLogger(__name__)

QUERY_TAG = "snowdesk"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Connection(Protocol):
    """The slice of ``SnowflakeConnection`` SnowDesk relies on.

    Declared as a protocol so tests can inject a fake connection that simulates
    running, succeeded, failed and cancelled statuses (spec 11).
    """

    def cursor(self) -> Any: ...
    def close(self) -> None: ...
    def get_query_status_throw_if_error(self, sfqid: str) -> Any: ...
    def is_still_running(self, status: Any) -> bool: ...


Connector = Any


@dataclass(slots=True)
class ConnectParams:
    name: str
    role: str | None = None
    warehouse: str | None = None


def _default_connect(params: ConnectParams) -> Connection:
    import snowflake.connector

    overrides = {
        key: value
        for key, value in {"role": params.role, "warehouse": params.warehouse}.items()
        if value
    }
    return snowflake.connector.connect(  # type: ignore[no-any-return]
        connection_name=params.name,
        client_session_keep_alive=True,  # C5
        client_store_temporary_credential=True,  # C4: cache SSO id token in Keychain
        session_parameters={"QUERY_TAG": QUERY_TAG},  # Q7
        **overrides,
    )


class SnowflakeSession:
    """Owns one long-lived authenticated connection."""

    #: A browser SSO round-trip takes noticeably longer than a cached-token
    #: connect; used to detect that ALLOW_ID_TOKEN is probably off (spec 7.2).
    BROWSER_PROMPT_SECONDS = 2.5

    def __init__(self, connect_fn: Any = _default_connect) -> None:
        self._connect_fn = connect_fn
        self._conn: Connection | None = None
        self._params: ConnectParams | None = None
        self._slow_auth_count = 0
        self.state = ConnectionState.DISCONNECTED

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("Not connected")
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def connection_name(self) -> str | None:
        return self._params.name if self._params else None

    def connect(self, params: ConnectParams) -> SessionContext:
        """Open the connection, replacing any existing one."""
        self.close()
        self.state = ConnectionState.CONNECTING
        started = time.monotonic()
        try:
            self._conn = self._connect_fn(params)
        except BaseException:
            self.state = ConnectionState.ERROR
            raise
        elapsed = time.monotonic() - started
        if elapsed > self.BROWSER_PROMPT_SECONDS:
            self._slow_auth_count += 1
        self._params = params
        self.state = ConnectionState.CONNECTED
        return self.read_context()

    def reconnect(self) -> SessionContext:
        """Re-open using the last-used parameters (C6)."""
        if self._params is None:
            raise RuntimeError("No previous connection to reconnect to")
        return self.connect(self._params)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self.state = ConnectionState.DISCONNECTED
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            log.debug("Ignoring error while closing connection", exc_info=True)

    # -- context (spec 7.6) ------------------------------------------------

    def read_context(self) -> SessionContext:
        """Read role / warehouse / database / schema after a statement."""
        conn = self._conn
        if conn is None:
            return SessionContext()
        ctx = SessionContext(
            role=_attr(conn, "role"),
            warehouse=_attr(conn, "warehouse"),
            database=_attr(conn, "database"),
            schema=_attr(conn, "schema"),
        )
        if any((ctx.role, ctx.warehouse, ctx.database, ctx.schema)):
            return ctx
        return self._query_context()

    def _query_context(self) -> SessionContext:
        """Fallback when the connector's cached context looks stale."""
        try:
            cur = self.connection.cursor()
            try:
                cur.execute(
                    "SELECT CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
                    "CURRENT_DATABASE(), CURRENT_SCHEMA()"
                )
                row = cur.fetchone()
            finally:
                cur.close()
        except Exception:
            log.debug("Could not read session context", exc_info=True)
            return SessionContext()
        if not row:
            return SessionContext()
        return SessionContext(*(str(v) if v else None for v in row[:4]))

    # -- SSO hint ----------------------------------------------------------

    def should_hint_id_token(self) -> bool:
        """True once repeated browser prompts suggest ALLOW_ID_TOKEN is off."""
        return self._slow_auth_count >= 2


def _attr(conn: Any, name: str) -> str | None:
    value = getattr(conn, name, None)
    return str(value) if value else None
