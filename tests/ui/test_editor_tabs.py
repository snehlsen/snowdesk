"""Editor tabs, .sql files and session restore (E2, E3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowdesk.storage.session import SessionStore
from snowdesk.ui import editor_tabs
from snowdesk.ui.editor_tabs import DIRTY_MARK, EditorTabs, SaveAnswer


@pytest.fixture
def tabs(qtbot, tmp_path):
    widget = EditorTabs(SessionStore(tmp_path / "session.json"))
    qtbot.addWidget(widget)
    widget.new_tab()
    return widget


def titles(tabs: EditorTabs) -> list[str]:
    return [tabs.tabText(i) for i in range(tabs.count())]


def type_into(editor, text: str) -> None:
    """Type as a user does.

    setPlainText() replaces the document and leaves it unmodified, so it cannot
    stand in for typing when the dirty flag is what is under test.
    """
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    cursor.insertText(text)


# -- tabs -------------------------------------------------------------------


def test_new_tabs_are_numbered(tabs: EditorTabs) -> None:
    tabs.new_tab()
    assert titles(tabs) == ["Untitled 1", "Untitled 2"]
    assert tabs.currentIndex() == 1


def test_closing_a_clean_tab_needs_no_confirmation(tabs: EditorTabs) -> None:
    tabs.new_tab()
    assert tabs.close_tab(1)
    assert titles(tabs) == ["Untitled 1"]


def test_closing_the_last_tab_leaves_a_fresh_one(tabs: EditorTabs) -> None:
    tabs.close_tab(0)
    assert tabs.count() == 1
    assert tabs.editor.toPlainText() == ""


def test_an_edited_tab_is_marked_dirty(tabs: EditorTabs) -> None:
    type_into(tabs.editor, "select 1")
    assert titles(tabs) == ["Untitled 1" + DIRTY_MARK]


def test_closing_an_edited_tab_asks_first(tabs: EditorTabs, monkeypatch) -> None:
    type_into(tabs.editor, "select 1")
    monkeypatch.setattr(tabs, "ask_save_changes", lambda _name: SaveAnswer.CANCEL)
    assert not tabs.close_tab(0)
    assert tabs.count() == 1

    monkeypatch.setattr(tabs, "ask_save_changes", lambda _name: SaveAnswer.DONT_SAVE)
    assert tabs.close_tab(0)


def test_an_empty_edited_tab_closes_without_asking(tabs: EditorTabs, monkeypatch) -> None:
    """Typing and deleting again should not produce a prompt."""
    type_into(tabs.editor, "select 1")
    type_into(tabs.editor, "   ")
    monkeypatch.setattr(
        tabs, "ask_save_changes", lambda _name: pytest.fail("should not have asked")
    )
    tabs.new_tab()
    assert tabs.close_tab(0)


# -- files (E3) -------------------------------------------------------------


def test_open_reads_a_file_into_a_new_tab(tabs: EditorTabs, tmp_path: Path) -> None:
    path = tmp_path / "query.sql"
    path.write_text("select 1 from t")
    editor = tabs.open_file(path)
    assert editor is not None
    assert editor.toPlainText() == "select 1 from t"
    assert titles(tabs)[-1] == "query.sql"
    assert tabs.path_of(editor) == path
    assert not editor.document().isModified()


def test_opening_the_same_file_twice_reuses_its_tab(tabs: EditorTabs, tmp_path: Path) -> None:
    path = tmp_path / "query.sql"
    path.write_text("select 1")
    first = tabs.open_file(path)
    tabs.new_tab()
    again = tabs.open_file(path)
    assert again is first
    assert tabs.currentWidget() is first


def test_open_reports_an_unreadable_file(tabs: EditorTabs, tmp_path: Path, monkeypatch) -> None:
    shown: list[str] = []
    # Patched where it is used: warn() puts up a modal box, which would hang
    # an offscreen run waiting for a click that never comes.
    monkeypatch.setattr(editor_tabs, "warn", lambda _p, _t, msg: shown.append(msg))
    assert tabs.open_file(tmp_path / "does-not-exist.sql") is None
    assert shown and "does-not-exist.sql" in shown[0]
    assert tabs.count() == 1  # no empty tab left behind


def test_save_writes_and_clears_the_dirty_mark(tabs: EditorTabs, tmp_path: Path) -> None:
    path = tmp_path / "out.sql"
    type_into(tabs.editor, "select 1")
    assert tabs._write(tabs.editor, path)
    assert path.read_text() == "select 1"
    assert titles(tabs) == ["out.sql"]
    assert not tabs.editor.document().isModified()


def test_save_asks_for_a_name_the_first_time(tabs: EditorTabs, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "new.sql"
    monkeypatch.setattr(
        "snowdesk.ui.editor_tabs.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "")),
    )
    type_into(tabs.editor, "select 2")
    assert tabs.save()
    assert target.read_text() == "select 2"

    # Saving again reuses the path rather than asking.
    monkeypatch.setattr(
        "snowdesk.ui.editor_tabs.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: pytest.fail("should not have asked again")),
    )
    type_into(tabs.editor, "select 3")
    assert tabs.save()
    assert target.read_text() == "select 3"


def test_save_as_adds_the_sql_suffix(tabs: EditorTabs, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "snowdesk.ui.editor_tabs.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "noext"), "")),
    )
    type_into(tabs.editor, "select 1")
    assert tabs.save_as()
    assert (tmp_path / "noext.sql").exists()


def test_cancelling_the_save_dialog_saves_nothing(tabs: EditorTabs, monkeypatch) -> None:
    monkeypatch.setattr(
        "snowdesk.ui.editor_tabs.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")),
    )
    type_into(tabs.editor, "select 1")
    assert not tabs.save()
    assert tabs.editor.document().isModified()


# -- session restore (E2) ---------------------------------------------------


def test_unsaved_work_survives_a_relaunch(qtbot, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    first = EditorTabs(store)
    qtbot.addWidget(first)
    first.new_tab()
    type_into(first.editor, "select 'not saved anywhere'")
    first.new_tab()
    type_into(first.editor, "select 2")
    first.setCurrentIndex(0)
    assert first.save_session()

    second = EditorTabs(store)
    qtbot.addWidget(second)
    assert second.restore_session() == 2
    assert second.widget(0).toPlainText() == "select 'not saved anywhere'"
    assert second.widget(1).toPlainText() == "select 2"
    assert second.currentIndex() == 0


def test_a_saved_file_is_reread_from_disk(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text("select 1")
    store = SessionStore(tmp_path / "session.json")

    first = EditorTabs(store)
    qtbot.addWidget(first)
    first.open_file(path)
    first.save_session()

    path.write_text("select 999 -- changed outside SnowDesk")
    second = EditorTabs(store)
    qtbot.addWidget(second)
    second.restore_session()
    assert "changed outside SnowDesk" in second.widget(0).toPlainText()


def test_unsaved_edits_to_a_file_win_over_the_file(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text("select 1")
    store = SessionStore(tmp_path / "session.json")

    first = EditorTabs(store)
    qtbot.addWidget(first)
    editor = first.open_file(path)
    type_into(editor, "select 1 -- work in progress")
    first.save_session()

    second = EditorTabs(store)
    qtbot.addWidget(second)
    second.restore_session()
    assert "work in progress" in second.widget(0).toPlainText()
    assert second.widget(0).document().isModified()
    assert second.tabText(0).endswith(DIRTY_MARK)


def test_a_vanished_file_is_skipped_not_fatal(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "gone.sql"
    path.write_text("select 1")
    store = SessionStore(tmp_path / "session.json")

    first = EditorTabs(store)
    qtbot.addWidget(first)
    first.open_file(path)
    first.new_tab()
    type_into(first.editor, "select 2")
    first.save_session()
    path.unlink()

    second = EditorTabs(store)
    qtbot.addWidget(second)
    assert second.restore_session() == 1
    assert second.widget(0).toPlainText() == "select 2"


def test_restoring_nothing_still_gives_one_tab(qtbot, tmp_path: Path) -> None:
    tabs = EditorTabs(SessionStore(tmp_path / "session.json"))
    qtbot.addWidget(tabs)
    assert tabs.restore_session() == 0
    assert tabs.count() == 1


def test_the_cursor_position_comes_back(qtbot, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    first = EditorTabs(store)
    qtbot.addWidget(first)
    first.new_tab()
    type_into(first.editor, "select 1 from orders")
    cursor = first.editor.textCursor()
    cursor.setPosition(9)
    first.editor.setTextCursor(cursor)
    first.save_session()

    second = EditorTabs(store)
    qtbot.addWidget(second)
    second.restore_session()
    assert second.widget(0).textCursor().position() == 9
