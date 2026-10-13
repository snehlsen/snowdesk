"""The Session lifecycle: connecting, passphrases, losing a Session, Settle,
and the Session's Commit mode and Transaction.

Driven through the real worker and a fake connection, with the worker run
synchronously (Lanes.synchronous) and a scripted prompter answering for the
user.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.session_lifecycle import Phase, SessionLifecycle, SessionStatus
from snowdesk.db import stages as stage_ops
from snowdesk.db.lanes import Lanes
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import RunScriptJob, SnowflakeWorker
from snowdesk.model import Credentials, ObjectNode, StageKind, StageRef, StatementOutcome
from snowdesk.storage.history import HistoryStore
from tests.fakes import (
    FakeConnection,
    FakeProgrammingError,
    FakeStages,
    FakeStatement,
    ScriptedPrompter,
    start_session,
)

# The exact exceptions cryptography raises through the connector.
MISSING = TypeError("Password was not given but private key is encrypted")
WRONG = ValueError("Incorrect password, could not decrypt key")


class Dropped(Exception):
    """Stands in for the connector's transport-level error."""


class Clock:
    """The time the lifecycle sees; tests move it on by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 9, 0)

    def __call__(self) -> datetime:
        return self.now


class App:
    """A Session lifecycle over a real worker and a fake connection."""

    def __init__(
        self,
        connect_fn: Callable[[ConnectParams], FakeConnection],
        prompter: ScriptedPrompter,
    ) -> None:
        self.lanes = Lanes.synchronous()
        self.worker = SnowflakeWorker(
            session=SnowflakeSession(connect_fn=connect_fn), lanes=self.lanes
        )
        self.prompter = prompter
        self.history = HistoryStore(":memory:")
        self.clock = Clock()
        self.lifecycle = SessionLifecycle(
            self.worker, prompter, history=self.history, clock=self.clock
        )
        self.changes: list[SessionStatus] = []
        self.notices: list[str] = []
        self.ended: list[StatementOutcome] = []
        self.lifecycle.changed.connect(self.changes.append)
        self.lifecycle.notice.connect(self.notices.append)
        self.lifecycle.transaction_ended.connect(self.ended.append)

    @property
    def phase(self) -> Phase:
        return self.lifecycle.status.phase

    def run(self, sql: str) -> None:
        self.worker.submit(RunScriptJob(statements=split_sql(sql)))

    def held(self):
        """Keep the jobs submitted in the block waiting, as if still running."""
        return self.lanes.session.held()


@pytest.fixture
def conn() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def app(qapp, conn) -> App:
    return App(lambda _p: conn, ScriptedPrompter())


def encrypted_key(attempts: list[ConnectParams], conn: FakeConnection):
    """A connect function for a key that only 'right' unlocks."""

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params)
        if params.private_key_passphrase is None:
            raise MISSING
        if params.private_key_passphrase != "right":
            raise WRONG
        return conn

    return connect_fn


# -- connecting ---------------------------------------------------------------


def test_starting_connects_and_names_the_connection(app: App) -> None:
    start_session(app.lifecycle)
    assert [c.phase for c in app.changes] == [Phase.CONNECTING, Phase.CONNECTED]
    assert app.lifecycle.is_connected
    assert app.lifecycle.connection_name == "dev"


def test_a_connect_error_is_a_failed_session(qapp) -> None:
    def refuse(_p: ConnectParams) -> FakeConnection:
        raise FakeProgrammingError("Incorrect username or password", errno=390100)

    app = App(refuse, ScriptedPrompter())
    start_session(app.lifecycle)
    status = app.lifecycle.status
    assert status.phase is Phase.FAILED
    assert status.error is not None and status.error.errno == 390100
    assert app.prompter.passphrase_questions == []  # not mistaken for a key problem


def test_starting_is_ignored_while_a_session_is_open(app: App) -> None:
    start_session(app.lifecycle)
    app.lifecycle.start("other")
    assert app.lifecycle.connection_name == "dev"
    assert app.lifecycle.is_connected


def test_the_sso_hint_becomes_a_notice(app: App, monkeypatch) -> None:
    monkeypatch.setattr(app.worker.session, "should_hint_id_token", lambda: True)
    start_session(app.lifecycle)
    assert any("ALLOW_ID_TOKEN" in n for n in app.notices)


# -- encrypted private keys (C3) ----------------------------------------------


def test_an_encrypted_key_asks_for_its_passphrase_then_connects(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(encrypted_key(attempts, conn), ScriptedPrompter(passphrases=("right",)))
    start_session(app.lifecycle)
    assert app.prompter.passphrase_questions == [("dev", False)]
    assert Phase.AWAITING_PASSPHRASE in [c.phase for c in app.changes]
    assert app.lifecycle.is_connected
    assert attempts[-1].private_key_passphrase == "right"


def test_a_wrong_passphrase_asks_again_as_rejected(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(encrypted_key(attempts, conn), ScriptedPrompter(passphrases=("nope", "right")))
    start_session(app.lifecycle)
    assert app.prompter.passphrase_questions == [("dev", False), ("dev", True)]
    assert app.lifecycle.is_connected


def test_cancelling_the_passphrase_leaves_it_disconnected_and_retryable(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(encrypted_key(attempts, conn), ScriptedPrompter(passphrases=(None,)))
    start_session(app.lifecycle)
    assert app.phase is Phase.DISCONNECTED
    assert any("passphrase is required" in n for n in app.notices)

    app.prompter.passphrases = ["right"]
    start_session(app.lifecycle)
    assert app.lifecycle.is_connected


def test_the_passphrase_is_remembered_for_the_run(qapp, conn) -> None:
    """Disconnect then Connect must not ask again."""
    attempts: list[ConnectParams] = []
    app = App(encrypted_key(attempts, conn), ScriptedPrompter(passphrases=("right",)))
    start_session(app.lifecycle)
    app.lifecycle.end()
    start_session(app.lifecycle)
    assert len(app.prompter.passphrase_questions) == 1
    assert attempts[-1].private_key_passphrase == "right"
    assert app.lifecycle.is_connected


def test_a_rejected_passphrase_is_forgotten(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(encrypted_key(attempts, conn), ScriptedPrompter(passphrases=("right",)))
    start_session(app.lifecycle)
    app.lifecycle.end()

    # The key changed under us; the remembered passphrase no longer works.
    def changed_key(params: ConnectParams) -> FakeConnection:
        attempts.append(params)
        if params.private_key_passphrase != "new":
            raise WRONG
        return conn

    app.worker.session._connect_fn = changed_key
    app.prompter.passphrases = ["new"]
    start_session(app.lifecycle)
    assert app.prompter.passphrase_questions[-1] == ("dev", True)
    assert app.lifecycle.is_connected


# -- password and MFA connections with no password configured -----------------


EMPTY_PASSWORD = FakeProgrammingError("Password is empty", errno=251006)
WRONG_PASSWORD = FakeProgrammingError("Incorrect username or password was specified.", errno=390100)


def password_account(attempts: list[ConnectParams], conn: FakeConnection):
    """A connect function for an account that only 'right' signs in to."""

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params)
        if params.password is None:
            raise EMPTY_PASSWORD
        if params.password != "right":
            raise WRONG_PASSWORD
        return conn

    return connect_fn


def test_a_missing_password_asks_for_it_then_connects(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(
        password_account(attempts, conn),
        ScriptedPrompter(passwords=(Credentials("right", "123456"),)),
    )
    start_session(app.lifecycle)
    assert app.prompter.password_questions == [("dev", None)]
    assert Phase.AWAITING_PASSWORD in [c.phase for c in app.changes]
    assert app.lifecycle.is_connected
    assert (attempts[-1].password, attempts[-1].passcode) == ("right", "123456")
    assert app.prompter.passphrase_questions == []


def test_a_wrong_password_asks_again_with_snowflakes_reason(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(
        password_account(attempts, conn),
        ScriptedPrompter(passwords=(Credentials("nope"), Credentials("right"))),
    )
    start_session(app.lifecycle)
    assert app.prompter.password_questions == [
        ("dev", None),
        ("dev", "Incorrect username or password was specified."),
    ]
    assert app.lifecycle.is_connected


def test_cancelling_the_password_leaves_it_disconnected_and_retryable(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(password_account(attempts, conn), ScriptedPrompter(passwords=(None,)))
    start_session(app.lifecycle)
    assert app.phase is Phase.DISCONNECTED
    assert any("password is required" in n for n in app.notices)

    app.prompter.passwords = [Credentials("right")]
    start_session(app.lifecycle)
    assert app.lifecycle.is_connected


def test_every_connect_asks_for_the_password_and_passcode(qapp, conn) -> None:
    """Nothing typed into the prompt outlives the connect it was typed for."""
    attempts: list[ConnectParams] = []
    app = App(
        password_account(attempts, conn),
        ScriptedPrompter(
            passwords=(
                Credentials("right", "111111"),
                Credentials("right", "222222"),
                Credentials("right", "333333"),
            )
        ),
    )
    start_session(app.lifecycle)
    app.lifecycle.end()
    start_session(app.lifecycle)
    app.lifecycle.reconnect()
    assert app.prompter.password_questions == [("dev", None)] * 3
    assert [a.passcode for a in attempts if a.password] == ["111111", "222222", "333333"]
    # Each connect went out without a password first, so none was held over.
    assert [a.password for a in attempts] == [None, "right"] * 3
    assert app.lifecycle.is_connected


# -- MFA connections with the password in the config file --------------------


TOTP_NEEDED = FakeProgrammingError(
    "Failed to connect to DB: example.snowflakecomputing.com:443. "
    "Failed to authenticate: MFA with TOTP is required.",
    errno=394508,
    sqlstate="08001",
)
BAD_PASSCODE = FakeProgrammingError(
    "Failed to connect to DB: example.snowflakecomputing.com:443. "
    "Incorrect passcode was specified.",
    errno=390127,
    sqlstate="08001",
)


def configured_password(attempts: list[ConnectParams], conn: FakeConnection):
    """The password is in the file; Snowflake still wants passcode '123456'."""

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params)
        if params.passcode is None:
            raise TOTP_NEEDED
        if params.passcode != "123456":
            raise BAD_PASSCODE
        return conn

    return connect_fn


def test_a_configured_password_asks_only_for_the_passcode(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(configured_password(attempts, conn), ScriptedPrompter(passcodes=("123456",)))
    start_session(app.lifecycle)
    assert app.prompter.passcode_questions == [("dev", TOTP_NEEDED.msg)]
    assert app.prompter.password_questions == []
    assert Phase.AWAITING_PASSCODE in [c.phase for c in app.changes]
    assert app.lifecycle.is_connected
    # The file's password is left to the connector: SnowDesk never sends one.
    assert [(a.password, a.passcode) for a in attempts] == [(None, None), (None, "123456")]


def test_a_wrong_passcode_asks_again_with_snowflakes_reason(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(configured_password(attempts, conn), ScriptedPrompter(passcodes=("000000", "123456")))
    start_session(app.lifecycle)
    assert [reason for _c, reason in app.prompter.passcode_questions] == [
        TOTP_NEEDED.msg,
        BAD_PASSCODE.msg,
    ]
    assert app.lifecycle.is_connected


def test_cancelling_the_passcode_leaves_it_disconnected(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(configured_password(attempts, conn), ScriptedPrompter(passcodes=(None,)))
    start_session(app.lifecycle)
    assert app.phase is Phase.DISCONNECTED
    assert any("MFA passcode is required" in n for n in app.notices)


def test_every_connect_asks_for_a_fresh_passcode(qapp, conn) -> None:
    attempts: list[ConnectParams] = []
    app = App(configured_password(attempts, conn), ScriptedPrompter(passcodes=("123456", "123456")))
    start_session(app.lifecycle)
    app.lifecycle.end()
    start_session(app.lifecycle)
    assert len(app.prompter.passcode_questions) == 2
    assert attempts[-2].passcode is None  # nothing held over from the first connect


# -- losing the Session (spec 9) ----------------------------------------------


def test_a_dropped_connection_loses_the_session(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    conn.plan["select"] = FakeStatement(error=Dropped("Connection reset by peer"))
    contexts: list = []
    app.worker.context_changed.connect(contexts.append)
    app.run("select 1")

    status = app.lifecycle.status
    assert status.phase is Phase.LOST
    assert status.connection == "dev"
    assert "Connection reset by peer" in status.message
    assert not status.transaction_lost
    assert not app.worker.session.is_connected
    assert contexts[-1].role is None  # cleaned up, not left showing the old role


def test_a_lost_session_says_whether_a_transaction_went_with_it(
    app: App, conn: FakeConnection
) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    conn.status_error = FakeProgrammingError("Session no longer exists", errno=390104)
    app.run("select 1")
    assert app.lifecycle.status.transaction_lost


def test_an_ordinary_sql_error_keeps_the_session(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    conn.plan["boom"] = FakeStatement(
        error=FakeProgrammingError("Object BOOM does not exist", errno=2003)
    )
    app.run("select boom")
    assert app.lifecycle.is_connected


def test_only_the_first_report_of_a_loss_counts(app: App) -> None:
    """Two lanes can notice the same drop."""
    start_session(app.lifecycle)
    app.worker.session_lost.emit("dev", "Connection reset by peer")
    app.worker.session_lost.emit("dev", "Broken pipe")
    lost = [c for c in app.changes if c.phase is Phase.LOST]
    assert [c.message for c in lost] == ["Connection reset by peer"]


def test_reconnect_reopens_the_last_connection(qapp, conn: FakeConnection) -> None:
    attempts: list[str] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params.name)
        return conn

    app = App(connect_fn, ScriptedPrompter())
    start_session(app.lifecycle)
    app.worker.session_lost.emit("dev", "Connection reset by peer")
    app.lifecycle.reconnect()
    assert attempts == ["dev", "dev"]
    assert app.lifecycle.is_connected


def test_reconnect_without_a_previous_connection_says_so(app: App) -> None:
    assert not app.lifecycle.reconnect()
    assert app.notices == ["No connection to reconnect to."]


# -- losing the Session on the export and transfer lanes ------------------------

LANDING = StageRef(kind=StageKind.NAMED, name="LANDING", database="RAW", schema="PUBLIC")
GONE = FakeProgrammingError("Session no longer exists.", errno=390111)


@pytest.fixture
def lanes(qapp) -> tuple[App, FakeConnection]:
    """An app with one stage."""
    conn = FakeConnection()
    conn.stage = FakeStages(
        stages=[{"name": "LANDING", "database_name": "RAW", "schema_name": "PUBLIC"}]
    )
    app = App(lambda _p: conn, ScriptedPrompter())
    start_session(app.lifecycle)
    return app, conn


def test_a_transfer_that_loses_the_session_loses_it_for_the_app(lanes, tmp_path) -> None:
    app, conn = lanes
    local = tmp_path / "a.csv"
    local.write_text("a")
    plan = stage_ops.plan_upload(conn, "t1", LANDING, "", [str(local)])
    assert conn.stage is not None
    conn.stage.fail_on[1] = GONE
    finished: list = []
    app.worker.transfer_finished.connect(finished.append)

    app.worker.start_transfer(plan)

    assert finished[0].error  # the transfer still reports itself interrupted
    assert app.phase is Phase.LOST
    assert not app.worker.session.is_connected  # closed on the job queue's thread


def test_planning_that_loses_the_session_loses_it_for_the_app(lanes, tmp_path) -> None:
    app, conn = lanes
    assert conn.stage is not None

    def gone(_sql: str):
        raise GONE

    conn.stage.handle = gone  # type: ignore[method-assign]
    local = tmp_path / "a.csv"
    local.write_text("a")  # something to upload, so planning lists the stage
    app.worker.plan_upload("t1", LANDING, "", [str(local)])
    assert app.phase is Phase.LOST


def test_an_export_that_loses_the_session_loses_it_for_the_app(lanes, tmp_path) -> None:
    app, conn = lanes
    cols = [("N", 0, None, None, 38, 0, False)]
    conn.plan["from orders"] = FakeStatement(columns=cols, rows=[(1,)])
    ready: list = []
    app.worker.result_ready.connect(lambda result_id, *_rest: ready.append(result_id))
    app.run("select * from orders")
    conn.plan["result_scan"] = FakeStatement(error=GONE)

    app.worker.export_csv(ready[0], str(tmp_path / "out.csv"), page_size=100)
    assert app.phase is Phase.LOST


# -- Settle ---------------------------------------------------------------------


def test_ending_without_a_transaction_does_not_ask(app: App) -> None:
    start_session(app.lifecycle)
    assert app.lifecycle.end()
    assert app.prompter.settle_questions == []
    assert app.phase is Phase.DISCONNECTED
    assert not app.worker.session.is_connected


def test_ending_mid_transaction_asks_and_can_be_called_off(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")

    app.prompter.settle = None
    assert not app.lifecycle.end()
    assert app.lifecycle.is_connected
    assert conn.transaction_id is not None

    app.prompter.settle = False
    app.lifecycle.end()
    assert conn.executed[-1] == "ROLLBACK"
    assert app.phase is Phase.DISCONNECTED
    assert len(app.prompter.settle_questions) == 2


def test_a_failed_commit_leaves_the_session_open(app: App, conn: FakeConnection) -> None:
    """Choosing Commit must never end in a silent rollback."""
    start_session(app.lifecycle)
    app.run("begin")
    conn.plan["COMMIT"] = FakeStatement(
        error=FakeProgrammingError("Constraint violated", errno=100072)
    )
    app.prompter.settle = True
    app.lifecycle.end()
    assert app.lifecycle.is_connected
    assert conn.transaction_id is not None
    assert not app.lifecycle.settling
    assert any("left open" in n for n in app.notices)


def test_reconnecting_mid_transaction_settles_first(qapp, conn: FakeConnection) -> None:
    attempts: list[str] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params.name)
        return conn

    app = App(connect_fn, ScriptedPrompter(settle=True))
    start_session(app.lifecycle)
    app.run("begin")
    app.lifecycle.reconnect()
    assert "COMMIT" in conn.executed
    assert attempts == ["dev", "dev"]
    assert app.lifecycle.is_connected


def test_settle_with_nothing_open_acts_at_once(app: App) -> None:
    start_session(app.lifecycle)
    done: list[bool] = []
    assert app.lifecycle.settle("Switching", then=lambda: done.append(True))
    assert done == [True]
    assert app.prompter.settle_questions == []


def test_settle_acts_only_once_the_commit_has_gone_through(app: App) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    app.prompter.settle = True
    done: list[bool] = []
    with app.held():
        assert not app.lifecycle.settle("Switching", then=lambda: done.append(True))
        assert app.lifecycle.settling and done == []
    assert done == [True]
    assert not app.lifecycle.settling


# -- Commit mode and the Transaction ---------------------------------------------


def test_switching_the_commit_mode_switches_the_session(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    assert app.lifecycle.transaction.autocommit is True
    app.lifecycle.set_commit_mode(False)
    assert conn.autocommit is False
    assert app.lifecycle.transaction.autocommit is False
    assert app.prompter.settle_questions == []


def test_a_refused_commit_mode_switch_says_why(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    conn.plan["AUTOCOMMIT = FALSE"] = FakeStatement(
        error=FakeProgrammingError("Insufficient privileges", errno=3001)
    )
    app.lifecycle.set_commit_mode(False)
    assert app.lifecycle.transaction.autocommit is True
    assert any(
        "Could not change the commit mode" in n and "Insufficient privileges" in n
        for n in app.notices
    )


def test_switching_the_mode_mid_transaction_settles_first(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")

    app.prompter.settle = None
    app.lifecycle.set_commit_mode(False)
    assert conn.autocommit is True
    assert conn.transaction_id is not None

    app.prompter.settle = True
    app.lifecycle.set_commit_mode(False)
    assert conn.executed[-2:] == ["COMMIT", "ALTER SESSION SET AUTOCOMMIT = FALSE"]
    assert app.lifecycle.transaction.autocommit is False
    assert len(app.prompter.settle_questions) == 2


def test_commit_ends_the_transaction_and_goes_into_history(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    assert app.lifecycle.can_end_transaction

    assert app.lifecycle.commit()
    assert conn.executed[-1] == "COMMIT"
    assert not app.lifecycle.transaction.in_transaction
    assert not app.lifecycle.can_end_transaction
    assert [o.statement.sql for o in app.ended] == ["COMMIT"]
    assert [e.sql for e in app.history.recent()] == ["COMMIT"]
    assert app.prompter.settle_questions == []


def test_there_is_nothing_to_roll_back_without_a_transaction(
    app: App, conn: FakeConnection
) -> None:
    start_session(app.lifecycle)
    assert not app.lifecycle.roll_back()
    assert "ROLLBACK" not in conn.executed
    assert app.ended == []


def test_only_one_commit_or_roll_back_is_in_flight(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    with app.held():
        assert app.lifecycle.commit()
        assert not app.lifecycle.can_end_transaction
        assert not app.lifecycle.roll_back()
        assert not app.lifecycle.commit()
    assert conn.executed.count("COMMIT") == 1
    assert "ROLLBACK" not in conn.executed
    assert len(app.ended) == 1


def test_the_transaction_knows_when_it_opened(app: App) -> None:
    start_session(app.lifecycle)
    assert app.lifecycle.transaction.opened_at is None
    opened = app.clock.now
    app.run("begin")
    assert app.lifecycle.transaction.opened_at == opened

    app.clock.now = opened + timedelta(minutes=5)
    app.run("insert into t values (1)")
    assert app.lifecycle.transaction.opened_at == opened, "still the same Transaction"

    app.lifecycle.commit()
    assert app.lifecycle.transaction.opened_at is None


def test_settling_waits_for_the_users_own_commit(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    done: list[bool] = []
    with app.held():
        app.lifecycle.commit()
        assert not app.lifecycle.settle("Disconnecting", then=lambda: done.append(True))
        assert app.lifecycle.settling and done == []
    assert done == [True]
    assert app.prompter.settle_questions == []
    assert conn.executed.count("COMMIT") == 1


def test_settling_behind_a_failed_commit_does_not_go_ahead(app: App, conn: FakeConnection) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    conn.plan["COMMIT"] = FakeStatement(
        error=FakeProgrammingError("Constraint violated", errno=100072)
    )
    with app.held():
        app.lifecycle.commit()
        app.lifecycle.end()
    assert app.lifecycle.is_connected
    assert conn.transaction_id is not None
    assert any("left open" in n for n in app.notices)
    assert app.prompter.settle_questions == []


def test_a_commit_answered_after_the_session_was_lost_still_counts(app: App) -> None:
    start_session(app.lifecycle)
    app.run("begin")
    done: list[bool] = []
    with app.held():
        app.lifecycle.commit()
        app.lifecycle.settle("Disconnecting", then=lambda: done.append(True))
        app.worker.session_lost.emit("dev", "Connection reset by peer")
        assert not app.lifecycle.settling
    assert app.phase is Phase.LOST
    assert [o.statement.sql for o in app.ended] == ["COMMIT"]
    assert [e.sql for e in app.history.recent()] == ["COMMIT"]
    assert done == [], "nothing queued behind it runs once the Session is gone"
    assert not app.lifecycle.transaction.in_transaction


# -- what follows the Session ---------------------------------------------------


def test_the_object_cache_empties_when_the_session_ends(app: App) -> None:
    """Cleared on the way out, so a new Session never sees the old one's objects."""
    browser = BrowserController(app.worker, app.lifecycle)
    start_session(app.lifecycle)
    browser._on_nodes((), [ObjectNode(name="RAW", kind="database")])
    assert browser.cached(()) is not None
    app.worker.session_lost.emit("dev", "Connection reset by peer")
    assert browser.cached(()) is None
