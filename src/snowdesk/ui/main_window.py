"""The main window: toolbar, browser, editor, results, messages, status bar."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from snowdesk import config
from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.db.session import ConnectionState, ConnectParams
from snowdesk.db.worker import ConnectJob, DisconnectJob, SnowflakeWorker
from snowdesk.model import ColumnInfo, QueryError, RunStatus, SessionContext, StatementOutcome
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionStore
from snowdesk.ui.editor import SqlEditor
from snowdesk.ui.editor_tabs import EditorTabs
from snowdesk.ui.history_panel import HistoryPanel
from snowdesk.ui.object_tree import ObjectTree
from snowdesk.ui.result_view import ResultModel, ResultView
from snowdesk.util.formatting import format_duration

log = logging.getLogger(__name__)

_STATE_DOT = {
    ConnectionState.DISCONNECTED.value: ("○", "#8a8f98", "Disconnected"),
    ConnectionState.CONNECTING.value: ("◐", "#a15c00", "Connecting…"),
    ConnectionState.CONNECTED.value: ("●", "#1a7f37", "Connected"),
    ConnectionState.ERROR.value: ("●", "#e5534b", "Error"),
}


class MainWindow(QMainWindow):
    """Emits intents, renders state; it holds no Snowflake objects itself."""

    closing = Signal()

    def __init__(
        self,
        worker: SnowflakeWorker,
        query: QueryController,
        browser: BrowserController,
        history: HistoryStore,
        session: SessionStore | None = None,
        dark: bool = False,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.query = query
        self.browser = browser
        self.history = history
        self.session = session or SessionStore(config.session_path())

        self.setWindowTitle("SnowDesk")
        self.resize(1280, 820)

        self._results: dict[str, ResultView] = {}
        self._context = SessionContext()
        self._connections: dict[str, config.ConnectionInfo] = {}

        self._build_toolbar()
        self._build_central(dark=dark)
        self._build_statusbar()
        self._build_actions()
        self._connect_signals()

        self._populate_connections()
        self._set_state(ConnectionState.DISCONNECTED.value, "")

    @property
    def editor(self) -> SqlEditor:
        """The focused editor tab; most of the window only cares about this one."""
        return self.editors.editor

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize() * 0.8)
        self.addToolBar(bar)

        bar.addWidget(QLabel("Connection: ", self))
        self.connection_box = QComboBox(self)
        self.connection_box.setMinimumWidth(200)
        bar.addWidget(self.connection_box)

        self.connect_button = QPushButton("Connect", self)
        self.connect_button.clicked.connect(self._on_connect_clicked)
        bar.addWidget(self.connect_button)

        bar.addSeparator()
        self.state_label = QLabel("", self)
        bar.addWidget(self.state_label)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self.run_button = QPushButton("Run", self)
        self.run_button.clicked.connect(self.run_current)
        bar.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.query.cancel)
        bar.addWidget(self.stop_button)

    def _build_central(self, dark: bool) -> None:
        # Left: object browser
        self.object_tree = ObjectTree(self.browser, self)
        self.tree_filter = QLineEdit(self)
        self.tree_filter.setPlaceholderText("filter…")
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.textChanged.connect(self.object_tree.filter_tree)
        refresh = QPushButton("⟳", self)
        refresh.setFixedWidth(30)
        refresh.setToolTip("Refresh objects")
        refresh.clicked.connect(lambda: self.object_tree.refresh())

        tree_top = QHBoxLayout()
        tree_top.setContentsMargins(6, 6, 6, 0)
        tree_top.addWidget(self.tree_filter, 1)
        tree_top.addWidget(refresh)

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addLayout(tree_top)
        left_layout.addWidget(self.object_tree)

        # Right: editor tabs over results
        self.editors = EditorTabs(self.session, self, dark=dark)
        self.editors.run_requested.connect(self.run_current)
        self.editors.run_all_requested.connect(self.run_all)
        self.editors.current_file_changed.connect(self._on_file_changed)
        self.editors.restore_session()

        self.result_tabs = QTabWidget(self)
        self.result_tabs.setDocumentMode(True)
        self.result_tabs.setTabsClosable(True)
        self.result_tabs.tabCloseRequested.connect(self._close_result_tab)

        self.messages = QPlainTextEdit(self)
        self.messages.setReadOnly(True)
        self.messages.setPlaceholderText("Statement results appear here.")
        self.result_tabs.addTab(self.messages, "Messages")

        self.history_panel = HistoryPanel(self.history, self)
        self.history_panel.statement_chosen.connect(self._load_from_history)
        self.result_tabs.addTab(self.history_panel, "History")
        self._pin_fixed_tabs()

        right = QSplitter(Qt.Orientation.Vertical, self)
        right.addWidget(self.editors)
        right.addWidget(self.result_tabs)
        right.setSizes([350, 420])
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 1)

        outer = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(left)
        outer.addWidget(right)
        outer.setSizes([260, 1020])
        outer.setStretchFactor(1, 1)
        self.setCentralWidget(outer)

    def _pin_fixed_tabs(self) -> None:
        """Messages and History have no close button — they are always there."""
        bar = self.result_tabs.tabBar()
        for index in range(self.result_tabs.count()):
            widget = self.result_tabs.widget(index)
            if widget in (self.messages, self.history_panel):
                for side in (bar.ButtonPosition.RightSide, bar.ButtonPosition.LeftSide):
                    bar.setTabButton(index, side, None)

    def _build_statusbar(self) -> None:
        self.context_label = QLabel("no context", self)
        self.rows_label = QLabel("", self)
        self.time_label = QLabel("", self)
        self.qid_label = QLabel("", self)
        for widget in (self.context_label, self.rows_label, self.time_label, self.qid_label):
            self.statusBar().addPermanentWidget(widget)
        self.statusBar().showMessage("Ready")

    def _build_actions(self) -> None:
        self._menus: dict[str, QMenu] = {}

        def action(text: str, shortcut: str, slot, menu_name: str) -> QAction:
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            # QAction.triggered carries a `checked` bool.  Connecting a slot
            # that takes optional arguments directly would receive it as the
            # first one -- new_tab(text=False) is how ⌘T stopped working -- so
            # the argument is dropped here, once, for every action.
            act.triggered.connect(lambda _checked=False, fn=slot: fn())
            self._menu(menu_name).addAction(act)
            self.addAction(act)
            return act

        action("Run Statement", "Ctrl+Return", self.run_current, "&Query")
        action("Run All", "Ctrl+Shift+Return", self.run_all, "&Query")
        self.cancel_action = action("Cancel", "Ctrl+.", self.query.cancel, "&Query")
        self.cancel_action.setEnabled(False)
        action("Reconnect", "Ctrl+R", self._on_connect_clicked, "&Query")
        action("New Tab", "Ctrl+T", self.editors.new_tab, "&File")
        action("Close Tab", "Ctrl+W", self._close_current_tab, "&File")
        action("Open…", "Ctrl+O", self.editors.open_file, "&File")
        action("Save", "Ctrl+S", self.editors.save, "&File")
        action("Save As…", "Ctrl+Shift+S", self.editors.save_as, "&File")
        action("Copy with Headers", "Ctrl+Shift+C", self._copy_with_headers, "&Edit")
        action("Clear History…", "", self._clear_history, "&Edit")

    def _menu(self, name: str) -> QMenu:
        menu = self._menus.get(name)
        if menu is None:
            menu = self.menuBar().addMenu(name)
            self._menus[name] = menu
        return menu

    # -- signal wiring -----------------------------------------------------

    def _connect_signals(self) -> None:
        w = self.worker
        w.state_changed.connect(self._set_state)
        w.context_changed.connect(self._set_context)
        w.connected.connect(self._on_connected)
        w.connect_failed.connect(self._on_connect_failed)
        w.passphrase_required.connect(self._on_passphrase_required)
        w.sso_hint.connect(self._on_sso_hint)
        w.statement_started.connect(self._on_statement_started)
        w.statement_finished.connect(self._on_statement_finished)
        w.result_ready.connect(self._on_result_ready)
        w.rows_appended.connect(self._on_rows_appended)
        w.fetch_failed.connect(self._on_fetch_failed)
        w.worker_error.connect(self._on_worker_error)

        self.query.run_started.connect(self._on_run_started)
        self.query.run_finished.connect(self._on_run_finished)
        self.query.rejected.connect(lambda msg: self.statusBar().showMessage(msg, 4000))

        self.object_tree.insert_requested.connect(self._insert_into_editor)
        self.object_tree.run_requested.connect(self._run_browser_sql)
        self.object_tree.status_message.connect(lambda msg: self.statusBar().showMessage(msg, 3000))

    # -- connections -------------------------------------------------------

    def _populate_connections(self) -> None:
        try:
            connections = config.list_connections()
        except config.ConfigError as exc:
            self._log_message(str(exc))
            connections = []
        self.connection_box.clear()
        if not connections:
            self.connection_box.addItem("(no connections found)")
            self.connection_box.setEnabled(False)
            self.connect_button.setEnabled(False)
            self._show_missing_config_hint()
            return
        self._connections = {c.name: c for c in connections}
        for conn in connections:
            label = f"{conn.name} — {conn.summary}" if conn.summary else conn.name
            self.connection_box.addItem(label, conn.name)
        default_index = next(
            (i for i, c in enumerate(connections) if c.is_default),
            0,
        )
        self.connection_box.setCurrentIndex(default_index)

    def _show_missing_config_hint(self) -> None:
        self._log_message(
            "No connections found. SnowDesk reads the same files as the `snow` CLI:\n"
            f"  · {config.connections_file()} — one [connection_name] table per connection\n"
            f"  · {config.config_file()} — connections under [connections.connection_name]\n"
            "Create either one (mode 600) with at least one connection, then restart "
            "SnowDesk."
        )

    def _on_connect_clicked(self) -> None:
        name = self.connection_box.currentData()
        if not name:
            return
        if self.worker.session.is_connected and self.connect_button.text() == "Disconnect":
            self.worker.submit(DisconnectJob())
            return
        self.worker.submit(ConnectJob(params=ConnectParams(name=name)))

    def _on_connected(self, name: str, ctx: SessionContext) -> None:
        self.statusBar().showMessage(f"Connected to {name}", 4000)
        self._set_context(ctx)
        self.object_tree.load_roots()

    def _on_connect_failed(self, error: QueryError) -> None:
        self._log_message(f"Connection failed: {error.formatted()}")
        QMessageBox.warning(self, "Could not connect", error.message)

    def _on_passphrase_required(self, name: str, rejected: bool) -> None:
        """Ask for the private key passphrase and retry the connect (C3).

        The passphrase is held in memory for this run only so a reconnect does
        not ask again; SnowDesk never writes it anywhere (spec 5, Security).
        """
        info = self._connections.get(name)
        headline = (
            "Incorrect passphrase. Try again."
            if rejected
            else f"The private key for \u201c{name}\u201d is encrypted."
        )
        lines = [headline]
        if info and info.private_key_file:
            lines.append(f"Key: {info.private_key_file}")
        lines.extend(["", "Enter the passphrase to unlock it:"])

        passphrase, accepted = QInputDialog.getText(
            self,
            "Private key passphrase",
            "\n".join(lines),
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not passphrase:
            self._log_message(
                f"Connection to {name} cancelled: the private key passphrase is required."
            )
            self.statusBar().showMessage("Passphrase required to connect", 6000)
            return
        self.worker.submit(
            ConnectJob(params=ConnectParams(name=name, private_key_passphrase=passphrase))
        )

    def _on_sso_hint(self) -> None:
        self._log_message(
            "The browser opened again for SSO. Ask an account admin to set "
            "ALLOW_ID_TOKEN = TRUE so the cached token in the Keychain can be reused."
        )

    def _set_state(self, state: str, detail: str) -> None:
        glyph, color, text = _STATE_DOT.get(state, ("○", "#8a8f98", state))
        self.state_label.setText(f'<span style="color:{color}">{glyph}</span> {text}')
        connected = state == ConnectionState.CONNECTED.value
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.connect_button.setEnabled(state != ConnectionState.CONNECTING.value)
        self.run_button.setEnabled(connected)
        if detail and state == ConnectionState.ERROR.value:
            self.statusBar().showMessage(detail, 8000)
        if not connected:
            self._set_context(SessionContext())

    def _set_context(self, ctx: SessionContext) -> None:
        self._context = ctx
        self.context_label.setText(str(ctx))

    # -- running -----------------------------------------------------------

    def run_current(self) -> None:
        start, end = self.editor.selection_range()
        self.query.run_selection(self.editor.toPlainText(), start, end)

    def run_all(self) -> None:
        self.query.run_text(self.editor.toPlainText())

    def _on_run_started(self, statements: list) -> None:
        self.editor.clear_error()
        self._clear_result_tabs()
        self.messages.clear()
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.cancel_action.setEnabled(True)
        count = len(statements)
        self._log_message(f"Running {count} statement{'' if count == 1 else 's'}…")

    def _on_statement_started(self, index: int, statement, query_id: str) -> None:
        label = f"[{index + 1}] running"
        if query_id:
            label += f" · {query_id}"
            self.qid_label.setText(query_id)
        self.statusBar().showMessage(label)

    def _on_statement_finished(self, outcome: StatementOutcome) -> None:
        prefix = f"[{outcome.index + 1}]"
        if outcome.status is RunStatus.ERROR and outcome.error is not None:
            self._log_message(f"{prefix} ERROR\n{outcome.error.formatted()}")
            self.editor.mark_error(outcome.statement.start, outcome.statement.end)
        elif outcome.status is RunStatus.CANCELLED:
            self._log_message(f"{prefix} Cancelled.")
        elif outcome.status is RunStatus.SKIPPED:
            self._log_message(f"{prefix} Skipped.")
        else:
            self._log_message(f"{prefix} {outcome.message}")
        if outcome.duration_s:
            self.time_label.setText(format_duration(outcome.duration_s))
        if outcome.query_id:
            self.qid_label.setText(outcome.query_id)

    def _on_run_finished(self, outcomes: list[StatementOutcome]) -> None:
        self.run_button.setEnabled(self.worker.session.is_connected)
        self.stop_button.setEnabled(False)
        self.cancel_action.setEnabled(False)
        self.history_panel.reload()
        failed = [o for o in outcomes if o.status is RunStatus.ERROR]
        cancelled = [o for o in outcomes if o.status is RunStatus.CANCELLED]
        if failed:
            self.statusBar().showMessage("Run failed", 6000)
            self.result_tabs.setCurrentWidget(self.messages)
        elif cancelled:
            self.statusBar().showMessage("Cancelled", 4000)
        else:
            self.statusBar().showMessage("Done", 4000)

    # -- results -----------------------------------------------------------

    def _on_result_ready(
        self,
        result_id: str,
        columns: list[ColumnInfo],
        rows: list,
        exhausted: bool,
        total: int | None,
    ) -> None:
        model = ResultModel(
            columns=columns,
            first_rows=rows,
            exhausted=exhausted,
            row_cap=self.query.row_cap,
            total=total,
        )
        view = ResultView(result_id, model, self)
        view.more_requested.connect(self.query.fetch_more)
        model.cap_reached.connect(lambda: self.rows_label.setText(model.status_text()))
        self._results[result_id] = view
        index = self.result_tabs.insertTab(
            max(0, self.result_tabs.count() - 2), view, f"Result {len(self._results)}"
        )
        self.result_tabs.setCurrentIndex(index)
        self.rows_label.setText(model.status_text())

    def _on_rows_appended(self, result_id: str, rows: list, exhausted: bool) -> None:
        view = self._results.get(result_id)
        if view is None:
            return
        view.append_rows(rows, exhausted)
        if self.result_tabs.currentWidget() is view:
            self.rows_label.setText(view.model.status_text())

    def _on_fetch_failed(self, result_id: str, message: str) -> None:
        view = self._results.get(result_id)
        if view is not None:
            view.model.mark_exhausted()
        self._log_message(f"Could not fetch more rows: {message}")

    def _close_result_tab(self, index: int) -> None:
        widget = self.result_tabs.widget(index)
        if widget in (self.messages, self.history_panel):
            return
        for result_id, view in list(self._results.items()):
            if view is widget:
                self.query.close_result(result_id)
                del self._results[result_id]
        self.result_tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()

    def _clear_result_tabs(self) -> None:
        for result_id, view in list(self._results.items()):
            self.query.close_result(result_id)
            index = self.result_tabs.indexOf(view)
            if index >= 0:
                self.result_tabs.removeTab(index)
            view.deleteLater()
        self._results.clear()
        self.rows_label.setText("")
        self.time_label.setText("")
        self.qid_label.setText("")

    def _copy_with_headers(self) -> None:
        view = self.result_tabs.currentWidget()
        if isinstance(view, ResultView):
            view.copy_selection(with_headers=True)
            self.statusBar().showMessage("Copied with headers", 2000)

    # -- misc --------------------------------------------------------------

    def _insert_into_editor(self, text: str) -> None:
        """Drop a name or generated statement into the focused tab (B3, B4)."""
        self.editor.insert_identifier(text)

    def _run_browser_sql(self, sql: str) -> None:
        """Run a statement the browser built, without disturbing the editor (B4)."""
        self.query.run_text(sql)

    def _close_current_tab(self) -> None:
        self.editors.close_tab(self.editors.currentIndex())

    def _on_file_changed(self, path: object) -> None:
        self.setWindowTitle(f"SnowDesk — {path}" if path else "SnowDesk")

    def _load_from_history(self, sql: str) -> None:
        self.editor.setPlainText(sql)
        self.editor.setFocus()

    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self, "Clear history", "Delete all locally stored query history?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self.history_panel.reload()

    def _on_worker_error(self, message: str) -> None:
        self._log_message(f"Internal error: {message}")
        self.statusBar().showMessage("An internal error occurred — see Messages", 6000)

    def _log_message(self, text: str) -> None:
        self.messages.appendPlainText(text)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Tab contents are autosaved, so quitting never has to ask (E2).
        self.editors.save_session()
        self.closing.emit()
        super().closeEvent(event)
