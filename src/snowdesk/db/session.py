"""The Snowflake connection (C3-C6).

Only this package imports ``snowflake.connector``.  A :class:`SnowflakeSession`
is owned by the worker thread and must not be touched from the UI thread.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from snowdesk.db.identifiers import quote_literal
from snowdesk.model import SessionContext

log = logging.getLogger(__name__)

QUERY_TAG = "snowdesk"

#: At login the connector fingerprints the environment to see whether it is
#: running on a cloud platform whose workload identity it could authenticate
#: with, and reports what it finds to Snowflake.  The AWS probe builds an STS
#: client, so botocore resolves whatever credentials it can find (hence the
#: "Found credentials in shared credentials file" line in the log) and calls
#: AWS with them on every connect.  SnowDesk promises no telemetry (spec 5),
#: so this is turned off -- but only as a default, so anyone who deliberately
#: sets the variable still gets what they asked for.
DISABLE_PLATFORM_DETECTION_VAR = "SNOWFLAKE_DISABLE_PLATFORM_DETECTION"


def disable_platform_detection() -> None:
    """Opt out of the connector's platform fingerprinting, unless overridden."""
    os.environ.setdefault(DISABLE_PLATFORM_DETECTION_VAR, "true")


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
    #: Passphrase for an encrypted ``private_key_file``, asked for at connect
    #: time.  ``repr=False`` keeps it out of logs and tracebacks; it is never
    #: written to disk (spec 5, Security).
    private_key_passphrase: str | None = field(default=None, repr=False)


def _default_connect(params: ConnectParams) -> Connection:
    import snowflake.connector

    disable_platform_detection()

    overrides = {
        key: value
        for key, value in {
            "role": params.role,
            "warehouse": params.warehouse,
            "private_key_file_pwd": params.private_key_passphrase,
        }.items()
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
        #: The last size looked up, keyed by the role and warehouse it was
        #: looked up for, so a script that changes neither costs no round trip.
        self._warehouse_size: tuple[str | None, str, str | None] | None = None
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
        self._warehouse_size = None
        self.state = ConnectionState.DISCONNECTED
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            log.debug("Ignoring error while closing connection", exc_info=True)

    # -- context (spec 7.6) ------------------------------------------------

    def read_context(self, recheck_warehouse: bool = False) -> SessionContext:
        """Read role / warehouse / database / schema after a statement.

        The warehouse size is asked of the server only when the role or
        warehouse changed, or ``recheck_warehouse`` says it may have (an
        ``ALTER WAREHOUSE ... SET WAREHOUSE_SIZE``).
        """
        conn = self._conn
        if conn is None:
            return SessionContext()
        ctx = SessionContext(
            role=_attr(conn, "role"),
            warehouse=_attr(conn, "warehouse"),
            database=_attr(conn, "database"),
            schema=_attr(conn, "schema"),
        )
        if not any((ctx.role, ctx.warehouse, ctx.database, ctx.schema)):
            ctx = self._query_context()
        if not ctx.warehouse:
            return ctx
        cached = self._warehouse_size
        if recheck_warehouse or cached is None or cached[:2] != (ctx.role, ctx.warehouse):
            cached = (ctx.role, ctx.warehouse, self._read_warehouse_size(ctx.warehouse))
            self._warehouse_size = cached
        return replace(ctx, warehouse_size=cached[2])

    def _read_warehouse_size(self, name: str) -> str | None:
        """The warehouse's size, or ``None`` if it could not be read."""
        try:
            cur = self.connection.cursor()
            try:
                cur.execute(f"SHOW WAREHOUSES LIKE {quote_literal(name)}")
                columns = [str(d[0]).lower() for d in cur.description or ()]
                rows = cur.fetchall()
            finally:
                cur.close()
        except Exception:
            log.debug("Could not read the size of warehouse %s", name, exc_info=True)
            return None
        if "name" not in columns or "size" not in columns:
            return None
        name_at, size_at = columns.index("name"), columns.index("size")
        # LIKE treats "_" as a wildcard and ignores case, and the connector
        # hands back the warehouse as it was configured ("compute_wh"), so
        # prefer the exact name and settle for a case-insensitive one.
        matches = sorted(
            (row for row in rows if str(row[name_at]).upper() == name.upper()),
            key=lambda row: row[name_at] != name,
        )
        size = matches[0][size_at] if matches else None
        return str(size) if size else None

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

    # -- transactions (Q10) ------------------------------------------------

    def read_autocommit(self) -> bool | None:
        """The session's AUTOCOMMIT, or ``None`` if it could not be read.

        Asked of the server, because ``connections.toml`` can set it and a
        script can change it, and the connector only learns of either
        sometimes.
        """
        row = self._fetch_one("SHOW PARAMETERS LIKE 'AUTOCOMMIT' IN SESSION")
        if not row or len(row) < 2:
            return None
        return str(row[1]).strip().lower() == "true"

    def read_transaction_id(self) -> str | None:
        """The open transaction's id, or ``None`` when there is none.

        Raises when the session could not be asked, so a failed read is not
        mistaken for "nothing open".
        """
        cur = self.connection.cursor()
        try:
            cur.execute("SELECT CURRENT_TRANSACTION()")
            row = cur.fetchone()
        finally:
            cur.close()
        value = row[0] if row else None
        return str(value) if value else None

    def set_autocommit(self, enabled: bool) -> None:
        cur = self.connection.cursor()
        try:
            cur.execute(f"ALTER SESSION SET AUTOCOMMIT = {'TRUE' if enabled else 'FALSE'}")
        finally:
            cur.close()

    def _fetch_one(self, sql: str) -> Any:
        try:
            cur = self.connection.cursor()
            try:
                cur.execute(sql)
                return cur.fetchone()
            finally:
                cur.close()
        except Exception:
            log.debug("Could not run %s", sql, exc_info=True)
            return None

    # -- SSO hint ----------------------------------------------------------

    def should_hint_id_token(self) -> bool:
        """True once repeated browser prompts suggest ALLOW_ID_TOKEN is off."""
        return self._slow_auth_count >= 2


def _attr(conn: Any, name: str) -> str | None:
    value = getattr(conn, name, None)
    return str(value) if value else None
