"""The Session lifecycle: one Session from connecting to being ended or lost.

See CONTEXT.md for the terms and docs/adr/0001 for why this lives on the UI
thread.  The worker is still the only thing that touches the session; it
reports what happened (connected, connect failed, session lost, the
Transaction changed, a COMMIT ended) and this module decides what that means:
which state the Session is in, whether to ask for a passphrase or a password,
when it is safe to end the Session, and what its Commit mode and open
Transaction are.

Everything here runs on the UI thread, so the window and the controllers read
:attr:`SessionLifecycle.status` instead of the worker's session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from snowdesk.db.session import ConnectParams
from snowdesk.db.worker import (
    ConnectJob,
    DisconnectJob,
    EndTransactionJob,
    SetAutocommitJob,
    SnowflakeWorker,
)
from snowdesk.model import (
    ConnectFailure,
    Credentials,
    QueryError,
    RunStatus,
    StatementOutcome,
    TransactionState,
)
from snowdesk.storage.history import HistoryStore


class Phase(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAITING_PASSPHRASE = "awaiting passphrase"
    AWAITING_PASSWORD = "awaiting password"
    AWAITING_PASSCODE = "awaiting passcode"
    CONNECTED = "connected"
    LOST = "lost"
    FAILED = "failed"


#: The phases between asking for a Session and having one (or not).
OPENING = frozenset(
    {Phase.CONNECTING, Phase.AWAITING_PASSPHRASE, Phase.AWAITING_PASSWORD, Phase.AWAITING_PASSCODE}
)


@dataclass(frozen=True, slots=True)
class SessionStatus:
    phase: Phase = Phase.DISCONNECTED
    #: The Connection this Session is (or was) made from; empty before the first.
    connection: str = ""
    #: Why the Session was lost, or why connecting failed.
    message: str = ""
    error: QueryError | None = None
    #: Lost with a Transaction open: its changes were not committed.
    transaction_lost: bool = False

    @property
    def is_connected(self) -> bool:
        return self.phase is Phase.CONNECTED

    @property
    def is_opening(self) -> bool:
        """Connecting, or waiting on the user to get connected."""
        return self.phase in OPENING


class Prompter(Protocol):
    """The questions only the user can answer."""

    def ask_passphrase(self, connection: str, rejected: bool) -> str | None:
        """The private key passphrase for ``connection``, or None to give up."""
        ...

    def ask_password(self, connection: str, rejected: str | None) -> Credentials | None:
        """The password (and MFA passcode) for ``connection``, or None to give up.

        ``rejected`` is Snowflake's reason for turning down the last attempt,
        or None the first time round.
        """
        ...

    def ask_passcode(self, connection: str, reason: str) -> str | None:
        """An MFA passcode for ``connection``, whose password is configured.

        ``reason`` is Snowflake's wording for why the sign-in needs one.
        """
        ...

    def ask_settle(self, reason: str) -> bool | None:
        """Commit (True), Roll back (False), or Cancel (None)."""
        ...


class SessionLifecycle(QObject):
    """Owns the Session state, passphrase memory, Reconnect, Settle, and the
    Session's Commit mode and open Transaction."""

    #: The Session state changed; carries the new :class:`SessionStatus`.
    changed = Signal(object)
    #: A line worth keeping in Messages.
    notice = Signal(str)
    #: The Commit mode or open Transaction changed, or whether it can be
    #: ended did; carries the :class:`TransactionState`.
    transaction_changed = Signal(object)
    #: A COMMIT or ROLLBACK finished, either way, whether the user or Settle
    #: asked for it; carries its :class:`StatementOutcome`.
    transaction_ended = Signal(object)

    def __init__(
        self,
        worker: SnowflakeWorker,
        prompter: Prompter | None = None,
        history: HistoryStore | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__()
        self.worker = worker
        #: Set by whoever can show dialogs; the window, in the app.
        self.prompter = prompter
        self.history = history
        self._clock = clock
        self._status = SessionStatus()
        # Key passphrases the user has entered this run, so Disconnect followed
        # by Connect does not ask again.  In memory only, never persisted, and
        # dropped as soon as one is rejected.  They deliberately outlive a
        # Disconnect: being asked again for a key the app already unlocked
        # reads as a fault, and a passphrase held in a Python str cannot be
        # wiped anyway, so dropping the reference buys less than it costs.
        self._passphrases: dict[str, str] = {}
        #: The passphrase sent with the connect in flight, kept once it works.
        self._trying: str | None = None
        # Unlike passphrases, a password (and MFA passcode) is never kept: it
        # is handed to the one connect it was typed for, and every connect
        # asks again.  A password opens the account itself, not just one key,
        # so it is not left sitting in memory between sign-ins.
        self._transaction = TransactionState()
        #: A COMMIT or ROLLBACK is queued and has not answered.  Only one at a
        #: time, so its answer never needs matching to whoever asked.
        self._ending = False
        #: What to do once the COMMIT or ROLLBACK in flight succeeds.
        self._after_settle: Callable[[], None] | None = None

        worker.connected.connect(self._on_connected)
        worker.connect_failed.connect(self._on_connect_failed)
        worker.session_lost.connect(self._on_session_lost)
        worker.transaction_changed.connect(self._on_transaction_changed)
        worker.transaction_ended.connect(self._on_transaction_ended)
        worker.autocommit_failed.connect(self._on_autocommit_failed)

    # -- state -------------------------------------------------------------

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status.is_connected

    @property
    def settling(self) -> bool:
        """:meth:`settle` is waiting on a COMMIT or ROLLBACK to answer."""
        return self._after_settle is not None

    @property
    def transaction(self) -> TransactionState:
        """The Commit mode and open Transaction, as the Session last reported them."""
        return self._transaction

    @property
    def can_end_transaction(self) -> bool:
        """A Transaction is open and no COMMIT or ROLLBACK is already in flight."""
        return self.is_connected and self._transaction.in_transaction and not self._ending

    @property
    def connection_name(self) -> str:
        """The Connection of the current Session, or of the last one."""
        return self._status.connection

    def _move(self, status: SessionStatus) -> None:
        self._status = status
        if not status.is_connected:
            self._after_settle = None
            if self._ending or self._transaction != TransactionState():
                self._ending = False
                self._set_transaction(TransactionState())
        self.changed.emit(status)

    def _set_transaction(self, state: TransactionState) -> None:
        self._transaction = state
        self.transaction_changed.emit(state)

    # -- intents -----------------------------------------------------------

    def start(self, connection: str, passphrase: str | None = None) -> None:
        """Open a Session from ``connection``.

        Ignored while a Session is open or being opened: :meth:`end` it, or
        :meth:`reconnect`, so an open Transaction is settled first.
        """
        if self._status.is_opening or self._status.is_connected:
            return
        self._open(connection, passphrase)

    def end(self) -> bool:
        """Disconnect: end the Session, settling an open Transaction first.

        Returns False when the user backs out of settling.
        """
        if not self.is_connected:
            return True
        return self.settle(
            "Disconnecting ends this session and its open transaction.", then=self._close
        )

    def reconnect(self) -> bool:
        """Open a new Session from the last Connection, settling the old one first."""
        connection = self._status.connection
        if not connection:
            self.notice.emit("No connection to reconnect to.")
            return False
        if not self.is_connected:
            self._open(connection, None)
            return True
        return self.settle(
            "Reconnecting ends this session and its open transaction.",
            then=lambda: self._open(connection, None),
        )

    def settle(self, reason: str, then: Callable[[], None]) -> bool:
        """Run ``then`` once no Transaction is open, asking the user how to end one.

        With nothing to settle ``then`` runs straight away and this returns
        True.  Otherwise the user picks Commit or Roll back and ``then`` runs
        only after it succeeds; if it fails the Session is left as it was, so
        a Disconnect never turns a Commit into a silent rollback.  A COMMIT or
        ROLLBACK the user already started is waited for the same way, without
        asking.  Returns False when ``then`` has not run yet, or never will
        because the user cancelled.
        """
        if not (self.is_connected and (self._ending or self._transaction.in_transaction)):
            then()
            return True
        if self._after_settle is not None:
            return False  # already settling; the first request wins
        if self._ending:
            self._after_settle = then
            return False
        assert self.prompter is not None, "settling needs a prompter"
        commit = self.prompter.ask_settle(reason)
        if commit is None:
            return False
        self._after_settle = then
        self._end_transaction(commit)
        return False

    def commit(self) -> bool:
        """Commit the open Transaction.  False when there is nothing to commit
        now, or a COMMIT or ROLLBACK is already in flight."""
        return self._end_transaction(commit=True) if self.can_end_transaction else False

    def roll_back(self) -> bool:
        """Roll back the open Transaction; False as for :meth:`commit`."""
        return self._end_transaction(commit=False) if self.can_end_transaction else False

    def set_commit_mode(self, autocommit: bool) -> None:
        """Switch between auto-commit and manual commit, settling first."""
        if not self.is_connected or self._transaction.autocommit is autocommit:
            return
        self.settle(
            "The commit mode can only change once the open transaction has ended.",
            then=lambda: self.worker.submit(SetAutocommitJob(enabled=autocommit)),
        )

    # -- internals ---------------------------------------------------------

    def _end_transaction(self, commit: bool) -> bool:
        self._ending = True
        self.transaction_changed.emit(self._transaction)
        self.worker.submit(EndTransactionJob(commit=commit))
        return True

    def _open(
        self, connection: str, passphrase: str | None, credentials: Credentials | None = None
    ) -> None:
        self._trying = passphrase or self._passphrases.get(connection)
        self._move(SessionStatus(Phase.CONNECTING, connection))
        self.worker.submit(
            ConnectJob(
                params=ConnectParams(
                    name=connection,
                    private_key_passphrase=self._trying,
                    password=credentials.password if credentials else None,
                    passcode=credentials.passcode if credentials else None,
                )
            )
        )

    def _close(self) -> None:
        self._move(SessionStatus(Phase.DISCONNECTED, self._status.connection))
        self.worker.submit(DisconnectJob())

    # -- what the worker reports -------------------------------------------

    def _on_connected(self, connection: str, _ctx: object, sso_hint: bool) -> None:
        if self._status.phase is not Phase.CONNECTING:
            return
        if self._trying:
            self._passphrases[connection] = self._trying
        self._trying = None
        self._move(SessionStatus(Phase.CONNECTED, connection))
        if sso_hint:
            self.notice.emit(
                "The browser opened again for SSO. Ask an account admin to set "
                "ALLOW_ID_TOKEN = TRUE so the cached token in the Keychain can be reused."
            )

    def _on_connect_failed(self, connection: str, kind: ConnectFailure, error: QueryError) -> None:
        if self._status.phase is not Phase.CONNECTING:
            return
        self._trying = None
        if kind is ConnectFailure.ERROR:
            self._move(SessionStatus(Phase.FAILED, connection, error.message, error))
            return
        if kind in (ConnectFailure.PASSWORD_NEEDED, ConnectFailure.PASSWORD_REJECTED):
            self._ask_password(connection, kind, error)
            return
        if kind is ConnectFailure.PASSCODE_NEEDED:
            self._ask_passcode(connection, error)
            return
        # The key is encrypted and either no passphrase was given or the one
        # we had is wrong; either way the user can still get connected.
        rejected = kind is ConnectFailure.PASSPHRASE_REJECTED
        if rejected:
            self._passphrases.pop(connection, None)
        self._move(SessionStatus(Phase.AWAITING_PASSPHRASE, connection))
        assert self.prompter is not None, "an encrypted key needs a prompter"
        passphrase = self.prompter.ask_passphrase(connection, rejected)
        if not passphrase:
            self._move(SessionStatus(Phase.DISCONNECTED, connection))
            self.notice.emit(
                f"Connection to {connection} cancelled: the private key passphrase is required."
            )
            return
        self._open(connection, passphrase)

    def _ask_password(self, connection: str, kind: ConnectFailure, error: QueryError) -> None:
        """No password is configured, or the one the user typed was turned down."""
        rejected = error.message if kind is ConnectFailure.PASSWORD_REJECTED else None
        self._move(SessionStatus(Phase.AWAITING_PASSWORD, connection))
        assert self.prompter is not None, "a password connection needs a prompter"
        credentials = self.prompter.ask_password(connection, rejected)
        if credentials is None or not credentials.password:
            self._move(SessionStatus(Phase.DISCONNECTED, connection))
            self.notice.emit(f"Connection to {connection} cancelled: a password is required.")
            return
        self._open(connection, None, credentials)

    def _ask_passcode(self, connection: str, error: QueryError) -> None:
        """The configured password worked; Snowflake wants an MFA passcode too.

        The password stays where it is: only the passcode is sent, and the
        connector takes the password from the file as before.
        """
        self._move(SessionStatus(Phase.AWAITING_PASSCODE, connection))
        assert self.prompter is not None, "an MFA connection needs a prompter"
        passcode = self.prompter.ask_passcode(connection, error.message)
        if not passcode:
            self._move(SessionStatus(Phase.DISCONNECTED, connection))
            self.notice.emit(f"Connection to {connection} cancelled: an MFA passcode is required.")
            return
        self._move(SessionStatus(Phase.CONNECTING, connection))
        self.worker.submit(ConnectJob(params=ConnectParams(name=connection, passcode=passcode)))

    def _on_session_lost(self, connection: str, message: str) -> None:
        """Any lane may report this, and several may report the same loss."""
        if not self.is_connected:
            return
        lost = SessionStatus(
            Phase.LOST,
            connection or self._status.connection,
            message,
            transaction_lost=self._transaction.in_transaction,
        )
        self._move(lost)
        # The worker owns the session, so it does the closing.
        self.worker.submit(DisconnectJob())

    def _on_transaction_changed(self, state: TransactionState) -> None:
        if not self.is_connected:
            return
        if not state.in_transaction:
            opened_at = None
        elif state.transaction_id == self._transaction.transaction_id:
            opened_at = self._transaction.opened_at
        else:
            opened_at = self._clock()
        self._set_transaction(replace(state, opened_at=opened_at))

    def _on_transaction_ended(self, outcome: StatementOutcome) -> None:
        """Also reported after the Session was lost: the outcome is still real."""
        if self.history is not None:
            self.history.record(outcome, self._status.connection)
        then, self._after_settle = self._after_settle, None
        if self._ending:
            self._ending = False
            self.transaction_changed.emit(self._transaction)
        self.transaction_ended.emit(outcome)
        if then is None:
            return
        if outcome.status is not RunStatus.SUCCESS:
            self.notice.emit(
                f"{outcome.statement.sql} failed, so the session was left open "
                "with its transaction."
            )
            return
        self._transaction = replace(self._transaction, transaction_id=None, opened_at=None)
        then()

    def _on_autocommit_failed(self, message: str) -> None:
        self.notice.emit(f"Could not change the commit mode: {message}")
