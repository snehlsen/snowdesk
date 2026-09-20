"""Drive the main window with the fake data layer (spec 11)."""

from __future__ import annotations

import queue
import sys

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QMenu

from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionStore
from snowdesk.ui.editor import SqlEditor
from snowdesk.ui.editor_tabs import SaveAnswer
from snowdesk.ui.main_window import CONTROL_SPACING, WINDOW_MARGIN, MainWindow
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
        session=SessionStore(tmp_path / "session.json"),
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
        session=SessionStore(tmp_path / "session.json"),
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


# -- editor tabs and shortcuts (M4: E2, E3, section 8) ----------------------


def shortcuts(window) -> dict[str, str]:
    """Every action's shortcut, keyed by action text."""
    return {
        a.text(): a.shortcut().toString() for a in window.actions() if not a.shortcut().isEmpty()
    }


def test_every_section_8_shortcut_is_bound(harness: Harness) -> None:
    bound = set(shortcuts(harness.window).values())
    # ⌘ maps to Ctrl in Qt's portable notation.
    expected = {
        "Ctrl+Return",  # run statement or selection
        "Ctrl+Shift+Return",  # run all
        "Ctrl+.",  # cancel
        "Ctrl+T",  # new tab
        "Ctrl+W",  # close tab
        "Ctrl+O",  # open
        "Ctrl+S",  # save
        "Ctrl+Shift+C",  # copy with headers
    }
    assert expected <= bound, f"missing: {expected - bound}"


def test_new_and_close_tab_actions_work(harness: Harness) -> None:
    window = harness.window
    assert window.editors.count() == 1
    window.editors.new_tab()
    assert window.editors.count() == 2
    window._close_current_tab()
    assert window.editors.count() == 1


def test_the_window_runs_the_focused_tab(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select 1")
    window.editors.new_tab()
    window.editor.setPlainText("select * from orders")

    window.run_all()
    harness.drain()
    assert harness.conn.executed[-1] == "select * from orders"


def test_the_title_follows_the_open_file(harness: Harness, tmp_path) -> None:
    path = tmp_path / "report.sql"
    path.write_text("select 1")
    harness.window.editors.open_file(path)
    assert "report.sql" in harness.window.windowTitle()


def test_history_loads_into_the_focused_tab(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select * from orders")
    window.run_all()
    harness.drain()

    window.editors.new_tab()
    window.history_panel._on_double_click(0, 0)
    assert window.editor.toPlainText() == "select * from orders"
    assert window.editors.count() == 2


def test_closing_the_window_saves_the_session(harness: Harness) -> None:
    window = harness.window
    cursor = window.editor.textCursor()
    cursor.insertText("select 'unsaved'")
    window.close()

    restored = window.session.load()
    assert [t.text for t in restored.tabs] == ["select 'unsaved'"]


def test_every_action_survives_the_checked_argument(harness: Harness, monkeypatch) -> None:
    """Qt passes `triggered(checked: bool)`; no slot may take it as data.

    Connecting a slot with optional parameters straight to `triggered` silently
    fed that bool in as the first argument, which is how ⌘T stopped working.
    Qt swallows exceptions raised inside a slot -- it prints them through
    sys.excepthook and carries on -- which is why the breakage was invisible,
    so the hook is what this asserts on.
    """
    nothing_chosen = staticmethod(lambda *a, **k: ("", ""))
    monkeypatch.setattr("snowdesk.ui.editor_tabs.QFileDialog.getOpenFileName", nothing_chosen)
    monkeypatch.setattr("snowdesk.ui.editor_tabs.QFileDialog.getSaveFileName", nothing_chosen)
    monkeypatch.setattr(
        harness.window.editors, "ask_save_changes", lambda _name: SaveAnswer.DONT_SAVE
    )
    monkeypatch.setattr(harness.window, "confirm_clear_history", lambda: False)

    escaped: list[str] = []
    monkeypatch.setattr(
        sys, "excepthook", lambda kind, value, tb: escaped.append(f"{kind.__name__}: {value}")
    )

    for act in harness.window.actions():
        act.trigger()  # exactly what a shortcut or menu item does
        assert not escaped, f"{act.text()} raised {escaped}"


def test_new_tab_action_actually_adds_a_tab(harness: Harness) -> None:
    window = harness.window
    before = window.editors.count()
    next(a for a in window.actions() if a.text() == "New Tab").trigger()
    assert window.editors.count() == before + 1
    assert window.editor.toPlainText() == ""  # not the checked bool


def test_a_failed_new_tab_leaves_no_stray_editor(harness: Harness) -> None:
    """The tab widget must never hold an editor that is not one of its pages."""
    tabs = harness.window.editors
    tabs.new_tab()
    strays = tabs.findChildren(SqlEditor)
    pages = [tabs.widget(i) for i in range(tabs.count())]
    assert sorted(map(id, strays)) == sorted(map(id, pages))


def test_closing_the_sole_tab_keeps_the_window_usable(harness: Harness) -> None:
    window = harness.window
    assert window.editors.count() == 1
    window.editors.tabCloseRequested.emit(0)
    assert window.editors.count() == 1
    # The replacement tab is a working editor, not a corpse.
    cursor = window.editor.textCursor()
    cursor.insertText("select 1")
    assert window.editor.toPlainText() == "select 1"
    assert window.editors.capture_session().tabs[0].text == "select 1"


def test_closing_the_sole_tab_with_the_shortcut(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(
        harness.window.editors, "ask_save_changes", lambda _name: SaveAnswer.DONT_SAVE
    )
    window = harness.window
    cursor = window.editor.textCursor()
    cursor.insertText("select 1")
    window._close_current_tab()
    assert window.editors.count() == 1
    assert window.editor.toPlainText() == ""


def test_run_current_at_end_of_line_runs_that_line(harness: Harness) -> None:
    """⌘↩ with the cursor just past a semicolon ran the following statement."""
    connect(harness)
    window = harness.window
    text = "select 1;\nselect * from orders;"
    window.editor.setPlainText(text)

    cursor = window.editor.textCursor()
    cursor.setPosition(text.index("\n"))  # end of line 1, right after the ';'
    window.editor.setTextCursor(cursor)

    window.run_current()
    harness.drain()
    assert harness.conn.executed[-1] == "select 1"


def test_run_current_on_the_second_line_runs_the_second_statement(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    text = "select 1;\nselect * from orders;"
    window.editor.setPlainText(text)

    cursor = window.editor.textCursor()
    cursor.setPosition(len(text) - 1)  # end of line 2, before its ';'
    window.editor.setTextCursor(cursor)

    window.run_current()
    harness.drain()
    assert harness.conn.executed[-1] == "select * from orders"


def test_run_current_still_prefers_a_selection(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    text = "select 1;\nselect * from orders;"
    window.editor.setPlainText(text)

    cursor = window.editor.textCursor()
    cursor.setPosition(text.index("select * from orders"))
    cursor.setPosition(len(text) - 1, cursor.MoveMode.KeepAnchor)
    window.editor.setTextCursor(cursor)

    window.run_current()
    harness.drain()
    assert harness.conn.executed[-1] == "select * from orders"


# -- object browser context menu (M5: B3, B4) -------------------------------


def browse_to_orders(harness: Harness) -> None:
    """Populate the tree down to a table, through the controller's cache."""
    from snowdesk.db import browser as browse
    from snowdesk.model import ObjectNode

    controller = harness.window.browser
    controller._on_nodes((), [ObjectNode("RAW", browse.DATABASE, path=("RAW",))])
    controller._on_nodes(("RAW",), [ObjectNode("PUBLIC", browse.SCHEMA, path=("RAW", "PUBLIC"))])
    controller._on_nodes(
        ("RAW", "PUBLIC"),
        [ObjectNode("ORDERS", browse.TABLE, "Table", ("RAW", "PUBLIC", "ORDERS"))],
    )


def test_preview_runs_in_a_result_tab(harness: Harness) -> None:
    """M5 exit criterion: Preview opens a result tab without touching the editor."""
    connect(harness)
    harness.conn.plan["LIMIT 100"] = FakeStatement(columns=COLS, rows=[(1,), (2,)])
    browse_to_orders(harness)
    window = harness.window
    window.editor.setPlainText("-- my work in progress")

    window.object_tree._preview(("RAW", "PUBLIC", "ORDERS"))
    harness.drain()

    assert harness.conn.executed[-1] == "SELECT * FROM RAW.PUBLIC.ORDERS LIMIT 100"
    view = window.result_tabs.currentWidget()
    assert isinstance(view, ResultView)
    assert view.model.rowCount() == 2
    # The editor is untouched by a browser action.
    assert window.editor.toPlainText() == "-- my work in progress"


def test_show_ddl_runs_in_a_result_tab(harness: Harness) -> None:
    connect(harness)
    browse_to_orders(harness)
    from snowdesk.db import browser as browse

    harness.window.object_tree._show_ddl(("RAW", "PUBLIC", "ORDERS"), browse.TABLE)
    harness.drain()
    assert harness.conn.executed[-1] == "SELECT GET_DDL('TABLE', 'RAW.PUBLIC.ORDERS')"


def test_generate_select_lands_in_the_focused_tab(harness: Harness) -> None:
    browse_to_orders(harness)
    window = harness.window
    window.editors.new_tab()

    window.object_tree._generate_select(("RAW", "PUBLIC", "ORDERS"))
    assert window.editor.toPlainText() == "SELECT *\nFROM RAW.PUBLIC.ORDERS"
    assert window.editors.currentIndex() == 1


def test_double_click_inserts_into_the_editor(harness: Harness) -> None:
    browse_to_orders(harness)
    window = harness.window
    item = window.object_tree._item_for_path(("RAW", "PUBLIC", "ORDERS"))
    window.object_tree._on_double_clicked(item, 0)
    assert window.editor.toPlainText() == "RAW.PUBLIC.ORDERS"


def test_copy_name_reports_in_the_status_bar(harness: Harness) -> None:
    from snowdesk.db import browser as browse

    browse_to_orders(harness)
    harness.window.object_tree._copy_name(("RAW", "PUBLIC", "ORDERS"), browse.TABLE)
    assert "RAW.PUBLIC.ORDERS" in harness.window.statusBar().currentMessage()


# -- losing the connection (M7, spec 9) -------------------------------------


class _Dropped(Exception):
    """Stands in for the connector's transport error."""


def test_losing_the_session_shows_a_reconnect_banner(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select 'my work'")
    assert not window.banner_bar.isVisibleTo(window)

    harness.conn.plan["select"] = FakeStatement(error=_Dropped("Connection reset by peer"))
    window.run_all()
    harness.drain()

    assert window.banner_bar.isVisibleTo(window)
    assert "lost" in window.banner_label.text().lower()
    assert window.reconnect_button.isVisibleTo(window.banner)
    # An unexpected drop is the `error` state from C6, not a plain disconnect.
    assert "Error" in window.state_label.text()
    assert not window.run_button.isEnabled()
    # Editor contents survive a lost connection.
    assert window.editor.toPlainText() == "select 'my work'"


def test_the_banner_reconnects_in_one_click(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    harness.conn.plan["select"] = FakeStatement(error=_Dropped("Connection aborted"))
    window.editor.setPlainText("select 1")
    window.run_all()
    harness.drain()
    assert not harness.worker.session.is_connected

    del harness.conn.plan["select"]
    window.reconnect_button.click()
    harness.drain()

    assert harness.worker.session.is_connected
    assert not window.banner_bar.isVisibleTo(window)
    assert "Connected" in window.state_label.text()


def test_an_ordinary_sql_error_shows_no_banner(harness: Harness) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select boom")
    window.run_all()
    harness.drain()

    assert not window.banner_bar.isVisibleTo(window)
    assert harness.worker.session.is_connected
    assert "ERROR" in window.messages.toPlainText()


def test_the_banner_can_be_dismissed(harness: Harness) -> None:
    window = harness.window
    window.show_banner("something went wrong")
    assert window.banner_bar.isVisibleTo(window)
    window.dismiss_button.click()
    assert not window.banner_bar.isVisibleTo(window)


# -- macOS conventions ------------------------------------------------------


def test_menus_are_in_platform_order(harness: Harness) -> None:
    """macOS puts File and Edit first, app-specific menus after, Help last."""
    titles = [m.title().replace("&", "") for m in harness.window.menuBar().findChildren(QMenu)]
    assert titles == ["File", "Edit", "Query", "Help"]


def test_about_carries_the_menu_role_that_moves_it_to_the_app_menu(harness: Harness) -> None:
    about = next(a for a in harness.window.actions() if a.text() == "About SnowDesk")
    assert about.menuRole() == QAction.MenuRole.AboutRole


def test_menu_items_that_open_a_dialog_end_in_an_ellipsis(harness: Harness) -> None:
    opens_dialog = {"Open", "Save As", "Clear History"}
    for act in harness.window.actions():
        stem = act.text().removesuffix("…")
        if stem in opens_dialog:
            assert act.text().endswith("…"), f"{act.text()} should end with an ellipsis"


def test_clearing_history_is_confirmed_and_can_be_refused(harness: Harness, monkeypatch) -> None:
    connect(harness)
    window = harness.window
    window.editor.setPlainText("select 1")
    window.run_all()
    harness.drain()
    assert window.history.recent()

    monkeypatch.setattr(window, "confirm_clear_history", lambda: False)
    window._clear_history()
    assert window.history.recent(), "history was destroyed without consent"

    monkeypatch.setattr(window, "confirm_clear_history", lambda: True)
    window._clear_history()
    assert window.history.recent() == []


def test_the_window_title_is_the_file_name_not_its_path(harness: Harness, tmp_path) -> None:
    path = tmp_path / "quarterly report.sql"
    path.write_text("select 1")
    harness.window.editors.open_file(path)

    assert harness.window.windowTitle() == "quarterly report.sql"
    # The full path belongs to the proxy icon.
    assert harness.window.windowFilePath() == str(path)


def test_the_title_returns_to_the_app_name_for_an_unsaved_tab(harness: Harness, tmp_path) -> None:
    path = tmp_path / "q.sql"
    path.write_text("select 1")
    harness.window.editors.open_file(path)
    harness.window.editors.new_tab()
    assert harness.window.windowTitle() == "SnowDesk"
    assert harness.window.windowFilePath() == ""


def test_toolbar_buttons_are_labelled_for_assistive_tech(harness: Harness) -> None:
    for button in (harness.window.run_button, harness.window.stop_button):
        assert button.accessibleName()
        assert button.toolTip()


def test_status_bar_segments_are_separated(harness: Harness) -> None:
    dividers = [
        label for label in harness.window.statusBar().findChildren(QLabel) if label.text() == "│"
    ]
    assert len(dividers) == 3  # between four segments


def test_toolbar_content_is_inset_from_both_window_edges(harness: Harness) -> None:
    """Qt leaves toolbar content flush to the edge, which reads as cramped.

    QToolBarLayout recomputes its own margins, so the inset only holds as long
    as the controls live in a container whose layout we own.
    """
    window = harness.window
    window.resize(1000, 700)
    window.show()
    try:
        left = window.connection_box.parentWidget().mapTo(window, QPoint(0, 0)).x()
        first = window.connection_box.parentWidget().layout().itemAt(0).widget()
        first_left = first.mapTo(window, QPoint(0, 0)).x()
        last_right = window.stop_button.mapTo(window, QPoint(0, 0)).x() + window.stop_button.width()
        assert first_left - left >= WINDOW_MARGIN
        assert window.width() - last_right >= WINDOW_MARGIN
        # Symmetric, give or take the toolbar's own frame.
        assert abs(first_left - (window.width() - last_right)) <= 2
    finally:
        window.hide()


def test_neighbouring_controls_are_not_run_together(harness: Harness) -> None:
    row = harness.window.run_button.parentWidget().layout()
    assert row.spacing() == CONTROL_SPACING
    margins = row.contentsMargins()
    assert margins.left() == margins.right() == WINDOW_MARGIN
