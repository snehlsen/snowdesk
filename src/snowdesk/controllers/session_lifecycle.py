"""The Session lifecycle: one Session from connecting to being ended or lost.

See CONTEXT.md for the terms and docs/adr/0001 for why this lives on the UI
thread.  The worker is still the only thing that touches the session; it
reports what happened (connected, connect failed, session lost) and this
module decides what that means: which state the Session is in, whether to ask
for a passphrase, and when it is safe to end the Session.

Everything here runs on the UI thread, so the window and the controllers read
:attr:`SessionLifecycle.status` instead of the worker's session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from snowdesk.db.session import ConnectParams
from snowdesk.db.worker import ConnectJob, DisconnectJob, EndTransactionJob, SnowflakeWorker
from snowdesk.model import ConnectFailure, QueryError, RunStatus, StatementOutcome, TransactionState


class Phase(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAITING_PASSPHRASE = "awaiting passphrase"
    CONNECTED = "connected"
    LOST = "lost"
    FAILED = "failed"


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


class Prompter(Protocol):
    """The questions only the user can answer."""

    def ask_passphrase(self, connection: str, rejected: bool) -> str | None:
        """The private key passphrase for ``connection``, or None to give up."""
        ...

    def ask_settle(self, reason: str) -> bool | None:
        """Commit (True), Roll back (False), or Cancel (None)."""
        ...


class SessionLifecycle(QObject):
    """Owns the Session state, passphrase memory, Reconnect, and Settle."""

    #: The Session state changed; carries the new :class:`SessionStatus`.
    changed = Signal(object)
    #: A line worth keeping in Messages.
    notice = Signal(str)

    def __init__(self, worker: SnowflakeWorker, prompter: Prompter | None = None) -> None:
        super().__init__()
        self.worker = worker
        #: Set by whoever can show dialogs; the window, in the app.
        self.prompter = prompter
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
        self._transaction = TransactionState()
        #: What to do once the COMMIT or ROLLBACK queued by :meth:`settle` succeeds.
        self._after_settle: Callable[[], None] | None = None

        worker.connected.connect(self._on_connected)
        worker.connect_failed.connect(self._on_connect_failed)
        worker.session_lost.connect(self._on_session_lost)
        worker.transaction_changed.connect(self._on_transaction_changed)
        worker.transaction_ended.connect(self._on_transaction_ended)

    # -- state -------------------------------------------------------------

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status.is_connected

    @property
    def settling(self) -> bool:
        """A COMMIT or ROLLBACK queued by :meth:`settle` has not answered yet."""
        return self._after_settle is not None

    @property
    def connection_name(self) -> str:
        """The Connection of the current Session, or of the last one."""
        return self._status.connection

    def _move(self, status: SessionStatus) -> None:
        self._status = status
        if not status.is_connected:
            self._transaction = TransactionState()
            self._after_settle = None
        self.changed.emit(status)

    # -- intents -----------------------------------------------------------

    def start(self, connection: str, passphrase: str | None = None) -> None:
        """Open a Session from ``connection``.

        Ignored while a Session is open or being opened: :meth:`end` it, or
        :meth:`reconnect`, so an open Transaction is settled first.
        """
        if self._status.phase in (Phase.CONNECTING, Phase.AWAITING_PASSPHRASE, Phase.CONNECTED):
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
        a Disconnect never turns a Commit into a silent rollback.  Returns
        False when ``then`` has not run yet, or never will because the user
        cancelled.
        """
        if not (self.is_connected and self._transaction.in_transaction):
            then()
            return True
        if self._after_settle is not None:
            return False  # already settling; the first request wins
        assert self.prompter is not None, "settling needs a prompter"
        commit = self.prompter.ask_settle(reason)
        if commit is None:
            return False
        self._after_settle = then
        self.worker.submit(EndTransactionJob(commit=commit))
        return False

    # -- internals ---------------------------------------------------------

    def _open(self, connection: str, passphrase: str | None) -> None:
        self._trying = passphrase or self._passphrases.get(connection)
        self._move(SessionStatus(Phase.CONNECTING, connection))
        self.worker.submit(
            ConnectJob(params=ConnectParams(name=connection, private_key_passphrase=self._trying))
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
        if self.is_connected:
            self._transaction = state

    def _on_transaction_ended(self, outcome: StatementOutcome) -> None:
        then, self._after_settle = self._after_settle, None
        if then is None:
            return
        if outcome.status is not RunStatus.SUCCESS:
            self.notice.emit(
                f"{outcome.statement.sql} failed, so the session was left open "
                "with its transaction."
            )
            return
        self._transaction = replace(self._transaction, transaction_id=None)
        then()
