"""Drive the main window with the fake data layer (spec 11)."""

from __future__ import annotations

import queue

import pytest

from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from snowdesk.storage.history import HistoryStore
from snowdesk.ui.main_window import MainWindow
from snowdesk.ui.result_view import ResultView
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStatement

COLS = [("N", 0, None, None, 38, 0, False)]


class Harness:
    """Runs the worker's jobs synchronously so tests stay deterministic."""

    def __init__(self, window: MainWindow, worker: SnowflakeWorker, conn: FakeConnection) -> None:
        self.window = window
        self.worker = worker
        self.conn = conn

    def drain(self) -> None:
        while True:
            try:
                job = self.worker._queue.get_nowait()
            except queue.Empty:
                return
            self.worker._dispatch(job)


@pytest.fixture
def harness(qtbot, tmp_path, monkeypatch):
    cfg = tmp_path / "snowflake"
    cfg.mkdir()
    (cfg / "connections.toml").write_text('[dev]\naccount = "a"\nuser = "u"\n')
    monkeypatch.setenv("SNOWFLAKE_HOME", str(cfg))
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)

    conn = FakeConnection(
        {
            "from orders": FakeStatement(columns=COLS, rows=[(i,) for i in range(900)]),
            "boom": FakeStatement(
                error=FakeProgrammingError("Object BOOM does not exist", errno=2003)
            ),
            "wait": FakeStatement(columns=COLS, rows=[(1,)], polls=1000),
        }
    )
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    history = HistoryStore(tmp_path / "history.db")
    window = MainWindow(
        worker=worker,
        query=QueryController(worker, history=history),
        browser=BrowserController(worker),
        history=history,
    )
    qtbot.addWidget(window)
    yield Harness(window, worker, conn)
    history.close()


def connect(harness: Harness) -> None:
    harness.worker.submit(ConnectJob(params=ConnectParams(name="dev")))
    harness.drain()


def test_connection_picker_is_populated(harness: Harness) -> None:
    assert harness.window.connection_box.currentData() == "dev"
    assert harness.window.connection_box.isEnabled()


def test_run_button_is_disabled_until_connected(harness: Harness) -> None:
    assert not harness.window.run_button.isEnabled()
    connect(harness)
    assert harness.window.run_button.isEnabled()
    assert "RAW.PUBLIC" in harness.window.context_label.text()


def test_running_a_query_shows_rows(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select * from orders")
    window.run_all()
    harness.drain()

    view = window.result_tabs.currentWidget()
    assert isinstance(view, ResultView)
    assert view.model.rowCount() == 500  # first page only
    assert "500 of 900 rows" in window.rows_label.text()
    assert window.qid_label.text().startswith("01b0-")


def test_scrolling_loads_more_rows(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select * from orders")
    window.run_all()
    harness.drain()

    view = window.result_tabs.currentWidget()
    view.model.fetchMore()
    harness.drain()
    assert view.model.rowCount() == 900
    assert view.model.exhausted


def test_failing_statement_stops_the_run_and_is_underlined(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select 1;\nselect boom;\nselect 3;")
    window.run_all()
    harness.drain()

    messages = window.messages.toPlainText()
    assert "ERROR" in messages
    assert "[2003]" in messages
    assert "Skipped" in messages
    assert window.editor._error_range is not None
    start, end = window.editor._error_range
    assert window.editor.toPlainText()[start:end] == "select boom"


def test_cancel_reports_cancelled_without_an_error(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    harness.worker.statement_started.connect(lambda *_: harness.worker.cancel_running())
    window.editor.setPlainText("select system$wait(60) as wait")
    window.run_all()
    harness.drain()

    assert "Cancelled" in window.messages.toPlainText()
    assert "ERROR" not in window.messages.toPlainText()
    assert not window.stop_button.isEnabled()


def test_run_statement_under_cursor_runs_only_that_statement(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    text = "select 1;\nselect * from orders;"
    window.editor.setPlainText(text)
    cursor = window.editor.textCursor()
    cursor.setPosition(text.index("orders"))
    window.editor.setTextCursor(cursor)

    window.run_current()
    harness.drain()
    assert harness.conn.executed[-1] == "select * from orders"


def test_history_records_each_executed_statement(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select 1;\nselect * from orders;")
    window.run_all()
    harness.drain()

    entries = harness.window.history.recent()
    assert [e.sql for e in entries] == ["select * from orders", "select 1"]
    assert all(e.connection == "dev" for e in entries)
    assert window.history_panel.table.rowCount() == 2


def test_history_double_click_loads_the_statement(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select * from orders")
    window.run_all()
    harness.drain()

    window.editor.clear()
    window.history_panel._on_double_click(0, 0)
    assert window.editor.toPlainText() == "select * from orders"


def test_closing_a_result_tab_releases_the_cursor(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select * from orders")
    window.run_all()
    harness.drain()

    index = window.result_tabs.currentIndex()
    window._close_result_tab(index)
    harness.drain()
    assert window._results == {}
    assert len(harness.worker.results) == 0


def test_messages_and_history_tabs_cannot_be_closed(harness: Harness) -> None:
    window = harness.window
    before = window.result_tabs.count()
    window._close_result_tab(window.result_tabs.indexOf(window.messages))
    assert window.result_tabs.count() == before


# -- encrypted private keys (C3) -------------------------------------------


@pytest.fixture
def key_harness(qtbot, tmp_path, monkeypatch):
    """A connection whose private key is encrypted, unlocked by 'right'."""
    cfg = tmp_path / "snowflake"
    cfg.mkdir()
    (cfg / "config.toml").write_text(
        'default_connection_name = "keypair"\n'
        "[connections.keypair]\n"
        'account = "a"\n'
        'user = "u"\n'
        'authenticator = "SNOWFLAKE_JWT"\n'
        'private_key_file = "/keys/sf_rsa_key.p8"\n'
    )
    monkeypatch.setenv("SNOWFLAKE_HOME", str(cfg))
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)

    conn = FakeConnection()

    def connect_fn(params):
        if params.private_key_passphrase is None:
            raise TypeError("Password was not given but private key is encrypted")
        if params.private_key_passphrase != "right":
            raise ValueError("Incorrect password, could not decrypt key")
        return conn

    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=connect_fn))
    history = HistoryStore(tmp_path / "history.db")
    window = MainWindow(
        worker=worker,
        query=QueryController(worker, history=history),
        browser=BrowserController(worker),
        history=history,
    )
    qtbot.addWidget(window)
    yield Harness(window, worker, conn)
    history.close()


def answer_prompt(monkeypatch, *replies):
    """Queue answers for the passphrase dialog; each is (text, accepted)."""
    seen: list[str] = []
    answers = iter(replies)

    def fake_get_text(_parent, _title, label, _echo):
        seen.append(label)
        return next(answers)

    monkeypatch.setattr("snowdesk.ui.main_window.QInputDialog.getText", staticmethod(fake_get_text))
    return seen


def test_encrypted_key_prompts_and_then_connects(key_harness, monkeypatch) -> None:
    labels = answer_prompt(monkeypatch, ("right", True))
    key_harness.window._on_connect_clicked()
    key_harness.drain()

    assert key_harness.worker.session.is_connected
    assert "Connected" in key_harness.window.state_label.text()
    assert "encrypted" in labels[0]
    assert "/keys/sf_rsa_key.p8" in labels[0]


def test_wrong_passphrase_reprompts_then_succeeds(key_harness, monkeypatch) -> None:
    labels = answer_prompt(monkeypatch, ("wrong", True), ("right", True))
    key_harness.window._on_connect_clicked()
    key_harness.drain()

    assert len(labels) == 2
    assert "Incorrect passphrase" in labels[1]
    assert key_harness.worker.session.is_connected


def test_cancelling_the_prompt_leaves_the_app_usable(key_harness, monkeypatch) -> None:
    answer_prompt(monkeypatch, ("", False))
    key_harness.window._on_connect_clicked()
    key_harness.drain()

    window = key_harness.window
    assert not key_harness.worker.session.is_connected
    assert "Disconnected" in window.state_label.text()
    assert "passphrase is required" in window.messages.toPlainText()
    assert window.connect_button.isEnabled()  # can try again


def test_passphrase_is_not_asked_again_after_reconnect(key_harness, monkeypatch) -> None:
    labels = answer_prompt(monkeypatch, ("right", True))
    window = key_harness.window
    window._on_connect_clicked()
    key_harness.drain()

    window._on_connect_clicked()  # Disconnect
    key_harness.drain()
    window._on_connect_clicked()  # Connect again
    key_harness.drain()

    assert len(labels) == 1  # asked once for the whole run
    assert key_harness.worker.session.is_connected


def test_the_passphrase_never_reaches_the_messages_pane(key_harness, monkeypatch) -> None:
    answer_prompt(monkeypatch, ("hunter2", True), ("right", True))
    key_harness.window._on_connect_clicked()
    key_harness.drain()
    assert "hunter2" not in key_harness.window.messages.toPlainText()
