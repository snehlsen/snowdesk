"""The Session lifecycle: connecting, passphrases, losing a Session, and Settle.

Driven through the real worker and a fake connection, with the worker run
synchronously (Lanes.synchronous) and a scripted prompter answering for the
user.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.session_lifecycle import Phase, SessionLifecycle, SessionStatus
from snowdesk.db import stages as stage_ops
from snowdesk.db.lanes import Lanes
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import RunScriptJob, SnowflakeWorker
from snowdesk.model import ObjectNode, StageKind, StageRef
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
        self.lifecycle = SessionLifecycle(self.worker, prompter)
        self.changes: list[SessionStatus] = []
        self.notices: list[str] = []
        self.lifecycle.changed.connect(self.changes.append)
        self.lifecycle.notice.connect(self.notices.append)

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


# -- what follows the Session ---------------------------------------------------


def test_the_object_cache_empties_when_the_session_ends(app: App) -> None:
    """Cleared on the way out, so a new Session never sees the old one's objects."""
    browser = BrowserController(app.worker, app.lifecycle)
    start_session(app.lifecycle)
    browser._on_nodes((), [ObjectNode(name="RAW", kind="database")])
    assert browser.cached(()) is not None
    app.worker.session_lost.emit("dev", "Connection reset by peer")
    assert browser.cached(()) is None
