"""The Stages sidebar tab, driven through the main window (docs/stage-browser.md)."""

from __future__ import annotations

import gzip
import queue
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QSettings, Qt, QUrl
from PySide6.QtGui import QDropEvent

from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from snowdesk.model import StageKind
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionStore
from snowdesk.ui.main_window import MainWindow
from snowdesk.ui.stage_tree import (
    DATA_ROLE,
    FILE,
    FOLDER,
    KIND_ROLE,
    PATH_ROLE,
    STAGE,
    STAGE_ROLE,
    StagePanel,
)
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStages


class Immediately:
    """Stands in for the transfer thread pool: runs each job as it is submitted."""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait: bool = True) -> None:
        pass


class Harness:
    def __init__(self, window: MainWindow, worker: SnowflakeWorker, conn: FakeConnection):
        self.window = window
        self.worker = worker
        self.conn = conn
        assert conn.stage is not None
        self.stage = conn.stage

    @property
    def panel(self) -> StagePanel:
        return self.window.stage_panel

    def drain(self) -> None:
        while True:
            try:
                job = self.worker._queue.get_nowait()
            except queue.Empty:
                return
            self.worker._dispatch(job)

    def show_stages(self) -> None:
        self.window.sidebar.setCurrentWidget(self.panel)
        self.drain()

    def item(self, *labels: str):
        """Walk the tree by visible labels, expanding (and listing) as it goes."""
        tree = self.panel.tree
        found = None
        for label in labels:
            children = (
                [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
                if found is None
                else [found.child(i) for i in range(found.childCount())]
            )
            found = next((c for c in children if c is not None and c.text(0) == label), None)
            assert found is not None, f"no {label!r} among {[c.text(0) for c in children]}"
            found.setExpanded(True)
            self.drain()
        return found

    def labels(self, item) -> list[str]:
        return [item.child(i).text(0) for i in range(item.childCount())]

    def messages(self) -> str:
        return self.window.messages.toPlainText()


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """The sidebar remembers its page; keep that out of the real preferences."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings().clear()
    yield
    QSettings().clear()


@pytest.fixture
def harness(qtbot, tmp_path, monkeypatch):
    cfg = tmp_path / "snowflake"
    cfg.mkdir()
    (cfg / "connections.toml").write_text('[dev]\naccount = "a"\nuser = "u"\n')
    monkeypatch.setenv("SNOWFLAKE_HOME", str(cfg))
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)

    conn = FakeConnection()
    conn.stage = FakeStages(
        stages=[
            {"name": "LANDING", "database_name": "RAW", "schema_name": "PUBLIC"},
            {"name": "ARCHIVE", "database_name": "RAW", "schema_name": "PUBLIC"},
            {
                "name": "S3_EXPORTS",
                "database_name": "ANALYTICS",
                "schema_name": "OUT",
                "type": "EXTERNAL",
                "url": "s3://bucket/exports/",
            },
        ],
        files={
            "landing/2026-09/orders_01.csv.gz": b"x" * 1200,
            "landing/2026-09/orders_02.csv.gz": b"y" * 800,
            "landing/readme.txt": b"hello",
        },
    )
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    worker._transfer_pool = Immediately()  # type: ignore[assignment]
    history = HistoryStore(tmp_path / "history.db")
    window = MainWindow(
        worker=worker,
        query=QueryController(worker, history=history),
        browser=BrowserController(worker),
        history=history,
        session=SessionStore(tmp_path / "session.json"),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "ask_open_transaction", lambda _reason: False)
    monkeypatch.setattr(window, "ask_quit_during_transfer", lambda: True)
    assert window.sidebar.currentIndex() == 0  # Objects, with nothing remembered
    h = Harness(window, worker, conn)
    worker.submit(ConnectJob(params=ConnectParams(name="dev")))
    h.drain()
    yield h
    history.close()


# -- the tree -------------------------------------------------------------------


def test_stages_are_listed_only_once_the_tab_is_shown(harness: Harness) -> None:
    assert not any("STAGES" in sql.upper() for sql in harness.conn.executed)
    harness.show_stages()
    tree = harness.panel.tree
    tops = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert tops == ["@~", "ANALYTICS", "RAW"]
    schema = harness.item("RAW", "PUBLIC")
    assert harness.labels(schema) == ["ARCHIVE", "LANDING"]
    assert [sql for sql in harness.conn.executed if "STAGES" in sql] == ["SHOW STAGES IN ACCOUNT"]


def test_a_stage_lists_its_files_into_folders(harness: Harness) -> None:
    harness.show_stages()
    stage = harness.item("RAW", "PUBLIC", "LANDING")
    assert harness.labels(stage) == ["2026-09/", "readme.txt"]
    folder = stage.child(0)
    assert folder.data(0, KIND_ROLE) == FOLDER
    assert folder.text(1) == "2 files"
    month = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    assert harness.labels(month) == ["orders_01.csv.gz", "orders_02.csv.gz"]
    assert month.child(0).text(1) == "1.2 KB"
    assert month.child(0).data(0, PATH_ROLE) == "2026-09/orders_01.csv.gz"
    # One LIST for the stage; opening the folder reused it.
    assert [s for s in harness.conn.executed if s.startswith("LIST")] == [
        "LIST @RAW.PUBLIC.LANDING"
    ]


def test_a_listing_past_the_row_cap_says_so(harness: Harness) -> None:
    harness.window.stages.row_cap = 2
    harness.show_stages()
    stage = harness.item("RAW", "PUBLIC", "LANDING")
    assert any("row cap" in label for label in harness.labels(stage))


def test_an_external_stage_can_be_browsed_but_not_transferred(harness: Harness) -> None:
    harness.show_stages()
    stage = harness.item("ANALYTICS", "OUT", "S3_EXPORTS")
    assert stage.text(1) == "External"
    menu = harness.panel.build_context_menu(stage)
    assert menu is not None
    entries = [a.text() for a in menu.actions() if a.text()]
    assert "Upload Files…" not in entries and "Download…" not in entries
    harness.panel.tree.setCurrentItem(stage)
    assert not harness.panel.upload_button.isEnabled()
    assert "internal stage" in harness.panel.upload_button.toolTip()


def test_context_menus(harness: Harness) -> None:
    harness.show_stages()
    panel = harness.panel

    def entries(item) -> list[str]:
        menu = panel.build_context_menu(item)
        assert menu is not None
        return [a.text() for a in menu.actions() if a.text()]

    stage = harness.item("RAW", "PUBLIC", "LANDING")
    assert entries(stage) == [
        "Upload Files…",
        "Download…",
        "Copy Stage Path",
        "Insert Stage Path",
        "Generate COPY INTO",
        "Describe Stage",
        "Refresh",
    ]
    folder = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    assert "Delete…" in entries(folder)
    file = folder.child(0)
    assert entries(file) == [
        "Download…",
        "Delete…",
        "Copy Stage Path",
        "Insert Stage Path",
        "Generate COPY INTO",
        "Generate SELECT",
    ]


def test_generated_statements_go_to_the_editor(harness: Harness) -> None:
    harness.show_stages()
    file = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/").child(0)
    menu = harness.panel.build_context_menu(file)
    assert menu is not None
    next(a for a in menu.actions() if a.text() == "Generate COPY INTO").trigger()
    text = harness.window.editor.toPlainText()
    assert "COPY INTO <table>" in text
    assert "FILES = ('orders_01.csv.gz')" in text


def test_describe_stage_runs_in_a_result_tab(harness: Harness) -> None:
    harness.show_stages()
    stage = harness.item("RAW", "PUBLIC", "LANDING")
    menu = harness.panel.build_context_menu(stage)
    assert menu is not None
    next(a for a in menu.actions() if a.text() == "Describe Stage").trigger()
    harness.drain()
    assert "DESCRIBE STAGE RAW.PUBLIC.LANDING" in harness.conn.executed


# -- uploads --------------------------------------------------------------------


def drop(harness: Harness, item, paths: list[Path]) -> None:
    """Deliver a Finder drop onto ``item``, as Qt would."""
    tree = harness.panel.tree
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    point = tree.visualItemRect(item).center()
    event = QDropEvent(
        QPoint(point.x(), point.y()),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tree.dropEvent(event)
    harness.drain()


def test_dropping_files_on_a_folder_uploads_them_there(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    harness.show_stages()
    folder = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    new = tmp_path / "orders_03.csv"
    new.write_text("a,b\n")
    asked: list = []
    monkeypatch.setattr(harness.panel, "ask_replace", lambda plan: asked.append(plan))

    drop(harness, folder, [new])

    assert asked == []  # nothing in the way
    assert "landing/2026-09/orders_03.csv.gz" in harness.stage.files
    log = harness.messages()
    assert "Uploading 1 file to @RAW.PUBLIC.LANDING…" in log
    assert "uploaded   2026-09/orders_03.csv.gz" in log
    assert "Upload to @RAW.PUBLIC.LANDING finished: 1 uploaded." in log
    assert not harness.panel.progress_strip.isVisible()
    # The stage was re-listed and shows the new file.
    month = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    assert "orders_03.csv.gz" in harness.labels(month)
    # And the PUT is in History, like any statement.
    recorded = harness.window.history.recent()
    assert any(entry.sql.startswith("PUT 'file://") for entry in recorded)


def test_an_upload_that_would_overwrite_asks_first(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    harness.show_stages()
    folder = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    clash = tmp_path / "orders_01.csv"
    clash.write_text("new contents")
    answers = iter([None, False, True])  # Cancel, then Skip, then Replace
    seen: list = []

    def ask(plan):
        seen.append(list(plan.conflicts))
        return next(answers)

    monkeypatch.setattr(harness.panel, "ask_replace", ask)

    drop(harness, folder, [clash])
    assert harness.stage.files["landing/2026-09/orders_01.csv.gz"] == b"x" * 1200
    assert not harness.window.stages.is_busy  # Cancel left nothing in flight

    drop(harness, harness.item("RAW", "PUBLIC", "LANDING", "2026-09/"), [clash])
    assert harness.stage.files["landing/2026-09/orders_01.csv.gz"] == b"x" * 1200
    assert "skipped    2026-09/orders_01.csv.gz" in harness.messages()

    drop(harness, harness.item("RAW", "PUBLIC", "LANDING", "2026-09/"), [clash])
    stored = harness.stage.files["landing/2026-09/orders_01.csv.gz"]
    assert gzip.decompress(stored) == b"new contents"
    assert seen == [["2026-09/orders_01.csv.gz"]] * 3


def test_the_upload_button_uses_the_selected_folder(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    harness.show_stages()
    stage = harness.item("RAW", "PUBLIC", "LANDING")
    harness.panel.tree.setCurrentItem(stage)
    assert harness.panel.upload_button.isEnabled()
    new = tmp_path / "top.json"
    new.write_text("{}")
    monkeypatch.setattr(harness.panel, "ask_upload_files", lambda: [str(new)])
    harness.panel.upload_button.click()
    harness.drain()
    assert "landing/top.json.gz" in harness.stage.files


# -- downloads and deletes ---------------------------------------------------------


def test_download_writes_the_folder_with_its_structure(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    harness.show_stages()
    folder = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    target = tmp_path / "out"
    target.mkdir()
    monkeypatch.setattr(harness.panel, "ask_download_folder", lambda: str(target))
    harness.panel.tree.setCurrentItem(folder)
    harness.panel.download_action.trigger()
    harness.drain()
    assert sorted(p.name for p in (target / "2026-09").iterdir()) == [
        "orders_01.csv.gz",
        "orders_02.csv.gz",
    ]
    assert "a .gz file stays compressed" in harness.messages()


def test_delete_asks_and_then_removes_only_what_was_chosen(harness: Harness, monkeypatch) -> None:
    harness.show_stages()
    month = harness.item("RAW", "PUBLIC", "LANDING", "2026-09/")
    file = month.child(0)
    asked: list[list[str]] = []
    answer = iter([False, True])

    def confirm(_stage, labels):
        asked.append(labels)
        return next(answer)

    monkeypatch.setattr(harness.panel, "confirm_remove", confirm)
    harness.panel.tree.setCurrentItem(file)
    harness.panel.delete_selection(file)
    harness.drain()
    assert "landing/2026-09/orders_01.csv.gz" in harness.stage.files  # declined

    harness.panel.delete_selection(harness.item("RAW", "PUBLIC", "LANDING", "2026-09/").child(0))
    harness.drain()
    assert asked == [["2026-09/orders_01.csv.gz"]] * 2
    assert "landing/2026-09/orders_01.csv.gz" not in harness.stage.files
    assert "landing/2026-09/orders_02.csv.gz" in harness.stage.files


# -- table stages, connection, quitting ----------------------------------------------


def test_show_table_stage_from_the_objects_tree(harness: Harness) -> None:
    harness.window.show_table_stage(("RAW", "PUBLIC", "ORDERS"))
    harness.drain()
    assert harness.window.sidebar.currentWidget() is harness.panel
    item = harness.panel.tree.currentItem()
    assert item is not None and item.text(0) == "@%ORDERS"
    stage = item.data(0, STAGE_ROLE)
    assert stage.kind is StageKind.TABLE
    assert "LIST @RAW.PUBLIC.%ORDERS" in harness.conn.executed
    # It survives the stage list being refreshed.
    harness.panel.reload()
    harness.drain()
    assert "@%ORDERS" in harness.labels(harness.item("RAW", "PUBLIC"))


def test_disconnecting_clears_the_tree(harness: Harness) -> None:
    harness.show_stages()
    harness.window._on_connect_clicked()  # Disconnect
    harness.drain()
    tree = harness.panel.tree
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == [
        "Not connected"
    ]


def test_quitting_mid_transfer_asks_and_stops_it(harness: Harness, monkeypatch) -> None:
    window = harness.window
    window.stages._active = "t9"  # a transfer in flight
    stopped: list[bool] = []
    monkeypatch.setattr(window.worker, "stop_transfer", lambda: stopped.append(True))
    monkeypatch.setattr(window, "ask_quit_during_transfer", lambda: False)
    assert not window.close()
    assert stopped == []
    monkeypatch.setattr(window, "ask_quit_during_transfer", lambda: True)
    assert window.close()
    assert stopped == [True]


def test_the_panels_actions_survive_the_checked_argument(harness: Harness, monkeypatch) -> None:
    """The same sweep the window's own actions get."""
    monkeypatch.setattr(harness.panel, "ask_download_folder", lambda: "")
    escaped: list[str] = []
    monkeypatch.setattr(
        sys, "excepthook", lambda kind, value, tb: escaped.append(f"{kind.__name__}: {value}")
    )
    for act in harness.panel.tree.actions():
        act.trigger()
    harness.show_stages()
    harness.panel.tree.setCurrentItem(harness.item("RAW", "PUBLIC", "LANDING"))
    for act in harness.panel.tree.actions():
        act.trigger()
    assert not escaped


def test_items_carry_what_the_menu_needs(harness: Harness) -> None:
    harness.show_stages()
    stage = harness.item("RAW", "PUBLIC", "LANDING")
    assert stage.data(0, KIND_ROLE) == STAGE
    file = stage.child(1)
    assert file.data(0, KIND_ROLE) == FILE
    assert file.data(0, DATA_ROLE).raw == "landing/readme.txt"


def test_show_table_stage_while_the_stage_list_is_loading(harness: Harness) -> None:
    """The list arriving afterwards must not throw the table stage away."""
    harness.window.sidebar.setCurrentWidget(harness.panel)  # SHOW STAGES queued, not run
    harness.window.show_table_stage(("RAW", "PUBLIC", "ORDERS"))
    harness.drain()
    item = harness.panel.tree.currentItem()
    assert item is not None and item.text(0) == "@%ORDERS"
    assert item.isExpanded()


def test_the_sidebar_remembers_its_page(harness: Harness, qtbot, tmp_path) -> None:
    harness.show_stages()
    worker = harness.worker
    again = MainWindow(
        worker=worker,
        query=QueryController(worker),
        browser=BrowserController(worker),
        history=harness.window.history,
        session=SessionStore(tmp_path / "session2.json"),
    )
    qtbot.addWidget(again)
    assert again.sidebar.currentWidget() is again.stage_panel


def test_a_listing_error_is_readable_in_full(harness: Harness, monkeypatch) -> None:
    """The sidebar cuts the row short; the tooltip and Messages do not."""
    harness.show_stages()
    error = FakeProgrammingError(
        "SQL compilation error:\nStage 'RAW.PUBLIC.ARCHIVE' does not exist or not authorized.",
        errno=2003,
        sqlstate="02000",
        sfqid="01b0-9999",
    )

    def refuse(sql: str):
        if sql.startswith("LIST"):
            raise error
        return None

    monkeypatch.setattr(harness.stage, "handle", refuse)
    stage = harness.item("RAW", "PUBLIC", "ARCHIVE")
    notice = stage.child(0)
    assert notice.text(0) == "[2003] (SQLSTATE 02000) SQL compilation error:"
    tip = notice.toolTip(0)
    assert "does not exist or not authorized" in tip and "Query ID: 01b0-9999" in tip
    log = harness.messages()
    assert "Could not list @RAW.PUBLIC.ARCHIVE: [2003]" in log
    assert "does not exist or not authorized" in log
