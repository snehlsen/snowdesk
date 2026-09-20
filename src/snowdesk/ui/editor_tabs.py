"""Editor tabs with autosave, restore and .sql files (E2, E3)."""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTabWidget, QWidget

from snowdesk.storage.session import SessionState, SessionStore, TabState
from snowdesk.ui.editor import SqlEditor

log = logging.getLogger(__name__)


class SaveAnswer(StrEnum):
    """What the user chose when asked about unsaved changes."""

    SAVE = "save"
    DONT_SAVE = "dont_save"
    CANCEL = "cancel"


AUTOSAVE_INTERVAL_MS = 3000
SQL_FILTER = "SQL files (*.sql);;All files (*)"
#: Marks a tab whose contents differ from the file (or were never saved).
DIRTY_MARK = " •"


class EditorTabs(QTabWidget):
    """A tab per editor, autosaved so a relaunch restores unsaved work."""

    run_requested = Signal()
    run_all_requested = Signal()
    current_file_changed = Signal(object)  # Path | None

    def __init__(
        self,
        store: SessionStore,
        parent: QWidget | None = None,
        dark: bool = False,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._dark = dark
        self._untitled_count = 0
        self._paths: dict[SqlEditor, Path | None] = {}
        self._pending_save = False

        self.setDocumentMode(True)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.tabCloseRequested.connect(self.close_tab)
        self.currentChanged.connect(self._on_current_changed)

        self._autosave = QTimer(self)
        self._autosave.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave.timeout.connect(self._autosave_tick)
        self._autosave.start()

    # -- current tab -------------------------------------------------------

    @property
    def editor(self) -> SqlEditor:
        """The focused editor, creating one if every tab was closed."""
        current = self.currentWidget()
        if not isinstance(current, SqlEditor):
            return self.new_tab()
        return current

    def path_of(self, editor: SqlEditor) -> Path | None:
        return self._paths.get(editor)

    def _index_of(self, editor: SqlEditor) -> int:
        return self.indexOf(editor)

    # -- opening and closing ----------------------------------------------

    def new_tab(
        self, text: str = "", path: Path | None = None, title: str | None = None
    ) -> SqlEditor:
        """Add a tab and focus it.

        The editor is built unparented and only handed to ``addTab`` once it is
        fully set up, so a failure part-way cannot leave a stray child widget
        behind inside the tab widget.
        """
        editor = SqlEditor(None, dark=self._dark)
        editor.setPlainText(text)
        editor.document().setModified(False)
        editor.run_requested.connect(self.run_requested)
        editor.run_all_requested.connect(self.run_all_requested)
        editor.textChanged.connect(self._mark_pending)
        # modificationChanged fires exactly when the flag flips.  Reading
        # isModified() from textChanged instead would sample a transient value
        # -- setPlainText clears the flag right after emitting -- and leave the
        # tab marked dirty when it is not.
        editor.document().modificationChanged.connect(
            lambda modified, e=editor: self._on_modification_changed(e, modified)
        )

        if title is None:
            if path is not None:
                title = path.name
            else:
                self._untitled_count += 1
                title = f"Untitled {self._untitled_count}"
        self._paths[editor] = path
        index = self.addTab(editor, title)  # reparents the editor
        self.setTabToolTip(index, str(path) if path else title)
        self.setCurrentIndex(index)
        editor.setFocus()
        self._pending_save = True
        return editor

    def close_tab(self, index: int) -> bool:
        """Close a tab, offering to save it first. Returns whether it closed."""
        editor = self.widget(index)
        if not isinstance(editor, SqlEditor):
            return False
        if not self._confirm_discard(editor):
            return False
        self._paths.pop(editor, None)
        self.removeTab(index)
        editor.deleteLater()
        if self.count() == 0:
            self.new_tab()
        self._pending_save = True
        return True

    def _confirm_discard(self, editor: SqlEditor) -> bool:
        """Ask before dropping unsaved text; an empty scratch tab just goes."""
        if not editor.document().isModified() or not editor.toPlainText().strip():
            return True
        name = self.tabText(self._index_of(editor)).removesuffix(DIRTY_MARK)
        answer = self.ask_save_changes(name)
        if answer is SaveAnswer.CANCEL:
            return False
        if answer is SaveAnswer.SAVE:
            return self.save(editor)
        return True

    def ask_save_changes(self, name: str) -> SaveAnswer:
        """Put macOS's save-changes question to the user.

        Worded and ordered the way the platform words it: a question about the
        document, informative text about the consequence, and "Don't Save"
        rather than "Discard".  Separate from the decision above so the
        decision can be exercised without a modal dialog.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Do you want to save the changes you made to \u201c{name}\u201d?")
        box.setInformativeText("Your changes will be lost if you don't save them.")
        dont_save = box.addButton("Don't Save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        save = box.addButton(QMessageBox.StandardButton.Save)
        box.setDefaultButton(save)
        box.setEscapeButton(cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is save:
            return SaveAnswer.SAVE
        if clicked is dont_save:
            return SaveAnswer.DONT_SAVE
        return SaveAnswer.CANCEL

    # -- files (E3) --------------------------------------------------------

    def open_file(self, path: Path | str | None = None) -> SqlEditor | None:
        """Open a ``.sql`` file in a new tab (⌘O)."""
        if path is None:
            chosen, _filter = QFileDialog.getOpenFileName(self, "Open SQL file", "", SQL_FILTER)
            if not chosen:
                return None
            path = chosen
        path = Path(path)

        existing = next((e for e, p in self._paths.items() if p == path), None)
        if existing is not None:
            self.setCurrentWidget(existing)
            return existing
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Could not open file", f"{path}\n\n{exc}")
            return None
        return self.new_tab(text=text, path=path)

    def save(self, editor: SqlEditor | None = None) -> bool:
        """Save the tab, asking for a name the first time (⌘S)."""
        editor = editor or self.editor
        path = self._paths.get(editor)
        if path is None:
            return self.save_as(editor)
        return self._write(editor, path)

    def save_as(self, editor: SqlEditor | None = None) -> bool:
        """Save the tab under a new name (⌘⇧S)."""
        editor = editor or self.editor
        current = self._paths.get(editor)
        suggested = str(current) if current else f"{self.tabText(self._index_of(editor))}.sql"
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Save SQL file", suggested.removesuffix(DIRTY_MARK), SQL_FILTER
        )
        if not chosen:
            return False
        path = Path(chosen)
        if path.suffix == "":
            path = path.with_suffix(".sql")
        return self._write(editor, path)

    def _write(self, editor: SqlEditor, path: Path) -> bool:
        try:
            path.write_text(editor.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save file", f"{path}\n\n{exc}")
            return False
        self._paths[editor] = path
        editor.document().setModified(False)
        index = self._index_of(editor)
        self.setTabText(index, path.name)
        self.setTabToolTip(index, str(path))
        self._pending_save = True
        self.current_file_changed.emit(self.path_of(self.editor))
        return True

    # -- dirty marking -----------------------------------------------------

    def _mark_pending(self) -> None:
        self._pending_save = True

    def _on_modification_changed(self, editor: SqlEditor, modified: bool) -> None:
        self._pending_save = True
        index = self._index_of(editor)
        if index < 0:
            return
        title = self.tabText(index)
        base = title.removesuffix(DIRTY_MARK)
        wanted = base + DIRTY_MARK if modified else base
        if wanted != title:
            self.setTabText(index, wanted)

    def _on_current_changed(self, _index: int) -> None:
        self._pending_save = True
        current = self.currentWidget()
        if isinstance(current, SqlEditor):
            self.current_file_changed.emit(self._paths.get(current))

    # -- session (E2) ------------------------------------------------------

    def _autosave_tick(self) -> None:
        if not self._pending_save and not self._any_modified():
            return
        self.save_session()

    def _any_modified(self) -> bool:
        return any(
            isinstance(w, SqlEditor) and w.document().isModified()
            for w in (self.widget(i) for i in range(self.count()))
        )

    def capture_session(self) -> SessionState:
        tabs: list[TabState] = []
        for index in range(self.count()):
            editor = self.widget(index)
            if not isinstance(editor, SqlEditor):
                continue
            path = self._paths.get(editor)
            modified = editor.document().isModified()
            tabs.append(
                TabState(
                    title=self.tabText(index).removesuffix(DIRTY_MARK),
                    path=str(path) if path else None,
                    # A saved, unmodified file is re-read from disk instead.
                    text=editor.toPlainText() if (modified or path is None) else None,
                    cursor=editor.textCursor().position(),
                )
            )
        return SessionState(tabs=tabs, current=max(0, self.currentIndex()))

    def save_session(self) -> bool:
        written = self.store.save(self.capture_session())
        if written:
            self._pending_save = False
        return written

    def restore_session(self) -> int:
        """Rebuild tabs from the last run; returns how many were restored."""
        state = self.store.load()
        restored = 0
        for tab in state.tabs:
            path = Path(tab.path) if tab.path else None
            text = tab.text
            if text is None and path is not None:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    log.info("Skipping restored tab, file unreadable: %s", path)
                    continue
            editor = self.new_tab(text=text or "", path=path, title=tab.title)
            editor.document().setModified(tab.text is not None and path is not None)
            self._restore_cursor(editor, tab.cursor)
            restored += 1
        if restored:
            self.setCurrentIndex(min(state.current, restored - 1))
        else:
            self.new_tab()
        self._pending_save = False
        return restored

    @staticmethod
    def _restore_cursor(editor: SqlEditor, position: int) -> None:
        cursor = editor.textCursor()
        cursor.setPosition(min(position, len(editor.toPlainText())))
        editor.setTextCursor(cursor)

    def confirm_close_all(self) -> bool:
        """On quit: the session is autosaved, so nothing needs confirming."""
        return self.save_session() or True
