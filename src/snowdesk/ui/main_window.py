"""The main window: toolbar, browser, editor, results, messages, status bar."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from snowdesk import __version__, config
from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.controllers.stages import StageController
from snowdesk.db import profile
from snowdesk.db.identifiers import qualify
from snowdesk.db.session import ConnectionState, ConnectParams
from snowdesk.db.worker import (
    ConnectJob,
    DisconnectJob,
    EndTransactionJob,
    ProfileJob,
    ReconnectJob,
    SetAutocommitJob,
    SnowflakeWorker,
)
from snowdesk.model import (
    ColumnInfo,
    QueryError,
    RunStatus,
    SessionContext,
    StatementOutcome,
    TransactionState,
)
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionStore
from snowdesk.ui import preferences, theme
from snowdesk.ui.dialogs import message_box, warn
from snowdesk.ui.editor import SqlEditor
from snowdesk.ui.editor_tabs import EditorTabs
from snowdesk.ui.history_panel import HistoryPanel
from snowdesk.ui.object_tree import ObjectTree
from snowdesk.ui.result_view import ResultModel, ResultView
from snowdesk.ui.stage_tree import StagePanel
from snowdesk.util.formatting import format_duration

log = logging.getLogger(__name__)

#: Inset from the window edge for chrome that runs the full width.  Qt leaves
#: toolbars flush to the edge, which reads as cramped next to the rest of the
#: window; macOS insets its toolbar content.
WINDOW_MARGIN = 12
#: Gap between neighbouring controls.  Qt's default toolbar spacing is 1px,
#: which runs adjacent buttons together.
CONTROL_SPACING = 8

_STATE_DOT = {
    ConnectionState.DISCONNECTED.value: ("○", "#8a8f98", "Disconnected"),
    ConnectionState.CONNECTING.value: ("◐", "#a15c00", "Connecting…"),
    ConnectionState.CONNECTED.value: ("●", "#1a7f37", "Connected"),
    ConnectionState.ERROR.value: ("●", "#e5534b", "Error"),
}

#: The open-transaction dot.  Amber that reads on both light and dark bars.
TRANSACTION_COLOR = "#d29922"
#: How often "Transaction open · 4m" re-counts its minutes.
TRANSACTION_TICK_MS = 30_000
#: Says the commit-mode segment opens a menu, in place of Qt's own arrow.
MENU_MARK = "▾"
#: Which sidebar page was showing, so it comes back that way.
SIDEBAR_KEY = "window/sidebar"


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
        stages: StageController | None = None,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.query = query
        self.browser = browser
        self.history = history
        self.stages = stages or StageController(worker, history=history)
        self.session = session or SessionStore(config.session_path())

        self.setWindowTitle("SnowDesk")
        self.resize(1280, 820)

        self._results: dict[str, ResultView] = {}
        #: Numbers the "Result N" tabs; profile tabs carry their own label and
        #: are left out of the count.
        self._result_seq = 0
        #: The last statement's query id, so Query Profile still has a target
        #: when the statement produced no grid -- DDL, DML, or an error.
        self._last_query_id = ""
        self._export_dialog: QProgressDialog | None = None
        self._context = SessionContext()
        self._connections: dict[str, config.ConnectionInfo] = {}
        self._escape_formulas = True
        self._connected = False
        self._transaction = TransactionState()
        #: When the open transaction was first seen, for its elapsed time.
        self._transaction_since: datetime | None = None

        self._build_toolbar()
        self._build_banner()
        self._build_central(dark=dark)
        self._build_statusbar()
        self._build_actions()
        self._connect_signals()

        self._populate_connections()
        self._apply_saved_preferences()
        self._set_state(ConnectionState.DISCONNECTED.value, "")

    @property
    def editor(self) -> SqlEditor:
        """The focused editor tab; most of the window only cares about this one."""
        return self.editors.editor

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        """Toolbar chrome, laid out inside one container widget.

        QToolBarLayout recomputes its own margins from the style, so setting
        them there does not stick and the controls sit flush against the window
        edge.  Everything lives in a container whose layout we do control,
        which is also how the banner below is built.
        """
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(bar.iconSize() * 0.8)
        self.addToolBar(bar)

        content = QWidget(bar)
        row = QHBoxLayout(content)
        row.setContentsMargins(WINDOW_MARGIN, 4, WINDOW_MARGIN, 4)
        row.setSpacing(CONTROL_SPACING)

        row.addWidget(QLabel("Connection:", content))
        self.connection_box = QComboBox(content)
        self.connection_box.setMinimumWidth(200)
        self.connection_box.setAccessibleName("Connection")
        row.addWidget(self.connection_box)

        self.connect_button = QPushButton("Connect", content)
        self.connect_button.clicked.connect(self._on_connect_clicked)
        row.addWidget(self.connect_button)

        row.addSpacing(CONTROL_SPACING)
        self.state_label = QLabel("", content)
        # This one is markup on purpose: the coloured dot is a span SnowDesk
        # writes itself, from the fixed table above.
        self.state_label.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.state_label)

        row.addStretch(1)

        self.run_button = QPushButton("Run", content)
        self.run_button.setToolTip("Run the statement under the cursor (⌘↩)")
        self.run_button.setAccessibleName("Run statement")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.run_current)
        row.addWidget(self.run_button)

        self.stop_button = QPushButton("Stop", content)
        self.stop_button.setToolTip("Cancel the running statement (⌘.)")
        self.stop_button.setAccessibleName("Cancel statement")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.query.cancel)
        row.addWidget(self.stop_button)

        bar.addWidget(content)

    def _build_banner(self) -> None:
        """A dismissible strip for connection trouble, with one-click Reconnect."""
        self.banner = QWidget(self)
        self.banner.setObjectName("connectionBanner")
        # Scoped to the strip by object name: an unscoped QWidget rule cascades
        # into the buttons and washes them out.  A translucent tint over
        # whatever the window colour is keeps this readable in either
        # appearance, and the text and buttons keep their palette colours.
        self.banner.setStyleSheet(
            "#connectionBanner {"
            " background: rgba(232, 150, 40, 0.20);"
            " border-bottom: 1px solid rgba(232, 150, 40, 0.45); }"
        )
        self.banner_label = QLabel("", self.banner)
        self.banner_label.setWordWrap(True)
        # The banner quotes the connector's own words for what went wrong.
        self.banner_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reconnect_button = QPushButton("Reconnect", self.banner)
        self.reconnect_button.clicked.connect(self._reconnect)
        self.dismiss_button = QPushButton("Dismiss", self.banner)
        self.dismiss_button.clicked.connect(self.hide_banner)

        layout = QHBoxLayout(self.banner)
        layout.setContentsMargins(WINDOW_MARGIN, 6, WINDOW_MARGIN, 6)
        layout.setSpacing(CONTROL_SPACING)
        layout.addWidget(self.banner_label, 1)
        layout.addWidget(self.reconnect_button)
        layout.addWidget(self.dismiss_button)

        # QToolBar.addWidget wraps the widget in a QWidgetAction and shows it,
        # so the strip is hidden by toggling the toolbar, not the widget.
        self.banner_bar = QToolBar("Banner", self)
        self.banner_bar.setMovable(False)
        self.banner_bar.setFloatable(False)
        self.banner_bar.addWidget(self.banner)
        self.addToolBarBreak()
        self.addToolBar(self.banner_bar)
        self.banner_bar.setVisible(False)

    def show_banner(self, message: str, *, offer_reconnect: bool = True) -> None:
        self.banner_label.setText(message)
        self.reconnect_button.setVisible(offer_reconnect)
        self.banner_bar.setVisible(True)

    def hide_banner(self) -> None:
        self.banner_bar.setVisible(False)

    def _reconnect(self) -> None:
        if not self._settle_transaction("Reconnecting ends this session and its open transaction."):
            return
        self.hide_banner()
        self.worker.submit(ReconnectJob())

    def _build_central(self, dark: bool) -> None:
        # Left: object browser
        self.object_tree = ObjectTree(self.browser, self)
        self.tree_filter = QLineEdit(self)
        self.tree_filter.setPlaceholderText("filter…")
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.textChanged.connect(self.object_tree.filter_tree)
        refresh = QPushButton(self)
        refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh.setFixedWidth(32)
        refresh.setToolTip("Refresh the object list")
        refresh.setAccessibleName("Refresh objects")
        refresh.clicked.connect(lambda: self.object_tree.refresh())

        tree_top = QHBoxLayout()
        tree_top.setContentsMargins(CONTROL_SPACING, CONTROL_SPACING, CONTROL_SPACING, 0)
        tree_top.setSpacing(CONTROL_SPACING // 2)
        tree_top.addWidget(self.tree_filter, 1)
        tree_top.addWidget(refresh)

        objects = QWidget(self)
        left_layout = QVBoxLayout(objects)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addLayout(tree_top)
        left_layout.addWidget(self.object_tree)

        # Stages get a page of their own beside Objects (docs/stage-browser.md).
        self.stage_panel = StagePanel(self.stages, self)
        self.sidebar = QTabWidget(self)
        self.sidebar.setDocumentMode(True)
        self.sidebar.addTab(objects, "Objects")
        self.sidebar.addTab(self.stage_panel, "Stages")
        saved = str(QSettings().value(SIDEBAR_KEY, 0))
        self.sidebar.setCurrentIndex(int(saved) if saved.isdigit() else 0)
        self.sidebar.currentChanged.connect(self._on_sidebar_changed)
        left = self.sidebar

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
        # Role, warehouse, database and schema are read back from the account,
        # so their text is the account's to choose.  A database called
        # "<b>PROD</b>" should read as that and not redecorate the status bar.
        self.context_label.setTextFormat(Qt.TextFormat.PlainText)
        self.rows_label = QLabel("", self)
        self.time_label = QLabel("", self)
        self.qid_label = QLabel("", self)
        self.qid_label.setToolTip("Query ID of the last statement")

        # Commit mode, and the open transaction when there is one (Q10).  A
        # button, because clicking it is how the mode is changed; its menu is
        # filled in with the Query menu's actions in _build_actions.
        self.commit_button = QToolButton(self)
        self.commit_button.setAutoRaise(True)
        self.commit_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.commit_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.commit_button.setAccessibleName("Commit mode")
        # Styled to read as one more status segment, not a push button: the
        # macOS bezel and its menu arrow crowd the text, and the platform
        # gives tool buttons a smaller font than the labels beside it.  The
        # translucent hover works on light and dark bars alike.
        font = self.context_label.font()
        # Marked as set, or Qt resolves it straight back to the button font.
        font.setPointSizeF(font.pointSizeF())
        self.commit_button.setFont(font)
        self.commit_button.setIconSize(QSize(8, 8))
        self.commit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.commit_button.setStyleSheet(
            "QToolButton { border: none; border-radius: 4px; padding: 1px 4px; }"
            " QToolButton:hover, QToolButton:pressed"
            " { background: rgba(128, 128, 128, 0.18); }"
            " QToolButton::menu-indicator { image: none; width: 0; }"
        )
        self._transaction_dot = self._dot_icon(TRANSACTION_COLOR)
        self._transaction_timer = QTimer(self)
        self._transaction_timer.setInterval(TRANSACTION_TICK_MS)
        self._transaction_timer.timeout.connect(self._render_transaction)

        # Separated, or the segments read as one run-on string
        # ("RAW.PUBLIC 0 rows 0 ms 01b0-0001").  Each divider belongs to the
        # segment after it and hides with it, so an empty segment does not
        # leave a dangling bar.
        self._status_dividers: dict[QWidget, QLabel] = {}
        for index, widget in enumerate(
            (
                self.context_label,
                self.rows_label,
                self.time_label,
                self.qid_label,
                self.commit_button,
            )
        ):
            if index:
                divider = QLabel("│", self)
                divider.setEnabled(False)
                divider.setVisible(False)
                self.statusBar().addPermanentWidget(divider)
                self._status_dividers[widget] = divider
            self.statusBar().addPermanentWidget(widget)
        self._set_status_visible(self.commit_button, False)

    def _set_status_segment(self, label: QLabel, text: str) -> None:
        label.setText(text)
        divider = self._status_dividers.get(label)
        if divider is not None:
            divider.setVisible(bool(text))

    def _set_status_visible(self, widget: QWidget, visible: bool) -> None:
        widget.setVisible(visible)
        divider = self._status_dividers.get(widget)
        if divider is not None:
            divider.setVisible(visible)

    def _dot_icon(self, color: str) -> QIcon:
        """A small filled circle, drawn at the screen's pixel density."""
        ratio = max(1.0, self.devicePixelRatioF())
        size = 8
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        return QIcon(pixmap)

    def _build_actions(self) -> None:
        self._menus: dict[str, QMenu] = {}
        # macOS orders menus File, Edit, then app-specific, then Help; the
        # menu bar follows creation order, so they are created up front.
        for name in ("&File", "&Edit", "&View", "&Query", "&Help"):
            self._menu(name)

        def action(
            text: str,
            shortcut: str,
            slot,
            menu_name: str,
            role: QAction.MenuRole = QAction.MenuRole.NoRole,
        ) -> QAction:
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            # QAction.triggered carries a `checked` bool.  Connecting a slot
            # that takes optional arguments directly would receive it as the
            # first one -- new_tab(text=False) is how ⌘T stopped working -- so
            # the argument is dropped here, once, for every action.
            act.triggered.connect(lambda _checked=False, fn=slot: fn())
            # A role tells Qt to move the item into the macOS application menu,
            # where the platform expects to find it.
            act.setMenuRole(role)
            self._menu(menu_name).addAction(act)
            self.addAction(act)
            return act

        action("New Tab", "Ctrl+T", self.editors.new_tab, "&File")
        action("Open…", "Ctrl+O", self.editors.open_file, "&File")
        self._menu("&File").addSeparator()
        action("Close Tab", "Ctrl+W", self._close_current_tab, "&File")
        action("Save", "Ctrl+S", self.editors.save, "&File")
        action("Save As…", "Ctrl+Shift+S", self.editors.save_as, "&File")

        # Apple's title case capitalises words of four letters or more.
        action("Copy With Headers", "Ctrl+Shift+C", self._copy_with_headers, "&Edit")
        action("Export to CSV…", "Ctrl+E", self.export_current_result, "&File")
        self._menu("&Edit").addSeparator()
        action("Clear History…", "", self._clear_history, "&Edit")

        action("Run Statement", "Ctrl+Return", self.run_current, "&Query")
        action("Run All", "Ctrl+Shift+Return", self.run_all, "&Query")
        self.cancel_action = action("Cancel", "Ctrl+.", self.query.cancel, "&Query")
        self.cancel_action.setEnabled(False)
        self._menu("&Query").addSeparator()
        action("Query Profile", "Ctrl+Shift+P", self.profile_current_query, "&Query")
        action("Copy Query ID", "", self.copy_current_query_id, "&Query")
        self._menu("&Query").addSeparator()
        self._build_transaction_actions(action)
        self._menu("&Query").addSeparator()
        action("Reconnect", "Ctrl+R", self._reconnect, "&Query")

        action(
            "Settings…",
            "Ctrl+,",
            self.open_preferences,
            "&Edit",
            QAction.MenuRole.PreferencesRole,
        )

        self.detail_action = action("Show Cell Detail", "Ctrl+I", self.toggle_cell_detail, "&View")
        self.detail_action.setCheckable(True)
        self._menu("&View").addSeparator()

        self._build_appearance_menu()

        action(
            "About SnowDesk",
            "",
            self._show_about,
            "&Help",
            QAction.MenuRole.AboutRole,
        )

    def _build_transaction_actions(self, action) -> None:
        """Commit mode and Commit / Roll Back, in the Query menu and behind
        the status-bar button (Q10).

        No shortcuts: a stray keystroke that commits or throws away someone's
        changes costs far more than reaching for the menu does.
        """
        group = QActionGroup(self)
        group.setExclusive(True)
        self.autocommit_action = action(
            "Auto-Commit", "", lambda: self.set_autocommit(True), "&Query"
        )
        self.manual_commit_action = action(
            "Manual Commit", "", lambda: self.set_autocommit(False), "&Query"
        )
        for act in (self.autocommit_action, self.manual_commit_action):
            act.setCheckable(True)
            group.addAction(act)
        self.commit_action = action("Commit", "", self.commit_transaction, "&Query")
        self.rollback_action = action("Roll Back", "", self.rollback_transaction, "&Query")

        menu = QMenu(self.commit_button)
        menu.addAction(self.autocommit_action)
        menu.addAction(self.manual_commit_action)
        menu.addSeparator()
        menu.addAction(self.commit_action)
        menu.addAction(self.rollback_action)
        self.commit_button.setMenu(menu)
        self._render_transaction()

    def _build_appearance_menu(self) -> None:
        """View ▸ Appearance, as a set of mutually exclusive choices."""
        submenu = self._menu("&View").addMenu("Appearance")
        group = QActionGroup(self)
        group.setExclusive(True)
        self._appearance_actions: dict[theme.Appearance, QAction] = {}

        current = theme.load()
        for appearance in theme.Appearance:
            act = QAction(appearance.label, self)
            act.setCheckable(True)
            act.setChecked(appearance is current)
            act.triggered.connect(
                lambda _checked=False, choice=appearance: self.set_appearance(choice)
            )
            group.addAction(act)
            submenu.addAction(act)
            self._appearance_actions[appearance] = act

    def set_appearance(self, appearance: theme.Appearance) -> None:
        """Switch appearance and remember the choice (S1)."""
        theme.save(appearance)
        theme.apply(appearance)
        act = self._appearance_actions.get(appearance)
        if act is not None:
            act.setChecked(True)
        # Qt repaints its own palette; SnowDesk's own colours are re-applied by
        # the colour-scheme handler, which fires for system changes too.
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Re-apply SnowDesk's own colours for the appearance now in force."""
        self.editors.set_dark(theme.is_dark())

    def _apply_saved_preferences(self) -> None:
        """Adopt the stored preferences without re-saving or re-theming.

        The appearance has already been applied before any widget was built.
        """
        prefs = preferences.load()
        self.query.page_size = prefs.page_size
        self.query.row_cap = prefs.row_cap
        self.stages.row_cap = prefs.row_cap
        self._set_escape_formulas(prefs.escape_formulas)
        self.editors.set_font_size(prefs.font_size)

    def open_preferences(self) -> None:
        """Show the settings sheet and apply whatever comes back (S1)."""
        chosen = self.ask_preferences()
        if chosen is not None:
            self.apply_preferences(chosen)

    def ask_preferences(self) -> preferences.Preferences | None:
        """Put the settings sheet up. Separate from applying the result, so the
        applying can be exercised without a modal dialog."""
        dialog = preferences.PreferencesDialog(self)
        if dialog.exec() != preferences.PreferencesDialog.DialogCode.Accepted:
            return None
        return dialog.values()

    def apply_preferences(self, prefs: preferences.Preferences) -> None:
        preferences.save(prefs)
        # Page size and row cap are read when a query runs, so a change lands
        # on the next run without rebuilding anything that already exists.
        self.query.page_size = prefs.page_size
        self.query.row_cap = prefs.row_cap
        self.stages.row_cap = prefs.row_cap
        self._set_escape_formulas(prefs.escape_formulas)
        self.editors.set_font_size(prefs.font_size)
        self.set_appearance(prefs.appearance)

    def _set_escape_formulas(self, escape: bool) -> None:
        """Apply the export preference to the grids already open, too."""
        self._escape_formulas = escape
        for view in self._results.values():
            view.set_escape_formulas(escape)

    def _show_about(self) -> None:
        # Qt's static about box would go native; this is the same box, built
        # by hand so it does not (see snowdesk.ui.dialogs).
        box = message_box(self, QMessageBox.Icon.NoIcon)
        box.setWindowTitle("About SnowDesk")
        icon = QApplication.windowIcon()
        if not icon.isNull():
            box.setIconPixmap(icon.pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<b>SnowDesk {__version__}</b><br><br>"
            "A lightweight macOS client for Snowflake.<br>"
            "Connections come from the same files the <code>snow</code> CLI uses."
        )
        box.exec()

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
        w.connection_lost.connect(self._on_connection_lost)
        w.sso_hint.connect(self._on_sso_hint)
        w.statement_started.connect(self._on_statement_started)
        w.statement_finished.connect(self._on_statement_finished)
        w.result_ready.connect(self._on_result_ready)
        w.rows_appended.connect(self._on_rows_appended)
        w.fetch_failed.connect(self._on_fetch_failed)
        w.profile_empty.connect(self._on_profile_empty)
        w.profile_failed.connect(self._on_profile_failed)
        w.worker_error.connect(self._on_worker_error)
        w.export_progress.connect(self._on_export_progress)
        w.export_finished.connect(self._on_export_finished)
        w.export_failed.connect(self._on_export_failed)
        w.export_cancelled.connect(self._on_export_cancelled)
        w.transaction_changed.connect(self._on_transaction_changed)
        w.transaction_ended.connect(self._on_transaction_ended)
        w.autocommit_failed.connect(self._on_autocommit_failed)

        self.query.run_started.connect(self._on_run_started)
        self.query.run_finished.connect(self._on_run_finished)
        self.query.rejected.connect(lambda msg: self.statusBar().showMessage(msg, 4000))

        app = QApplication.instance()
        if isinstance(app, QApplication):
            # Fires for an explicit switch and for the system appearance
            # changing while SnowDesk is running.
            app.styleHints().colorSchemeChanged.connect(lambda _scheme: self.refresh_theme())

        self.history_panel.profile_requested.connect(self.show_query_profile)
        self.history_panel.status_message.connect(
            lambda msg: self.statusBar().showMessage(msg, 3000)
        )

        self.object_tree.insert_requested.connect(self._insert_into_editor)
        self.object_tree.run_requested.connect(self._run_browser_sql)
        self.object_tree.status_message.connect(lambda msg: self.statusBar().showMessage(msg, 3000))
        self.object_tree.table_stage_requested.connect(self.show_table_stage)
        self.browser.failed.connect(self._on_browse_failed)

        self.stage_panel.insert_requested.connect(self._insert_into_editor)
        self.stage_panel.run_requested.connect(self._run_browser_sql)
        self.stage_panel.log_message.connect(self._log_message)
        self.stage_panel.status_message.connect(lambda msg: self.statusBar().showMessage(msg, 4000))

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
            if not self._settle_transaction(
                "Disconnecting ends this session and its open transaction."
            ):
                return
            self.worker.submit(DisconnectJob())
            return
        self.worker.submit(ConnectJob(params=ConnectParams(name=name)))

    def _on_connected(self, name: str, ctx: SessionContext) -> None:
        self.hide_banner()
        self.statusBar().showMessage(f"Connected to {name}", 4000)
        self._set_context(ctx)
        self.object_tree.load_roots()
        self.stage_panel.set_connected(True)
        if self.sidebar.currentWidget() is self.stage_panel:
            self.stage_panel.ensure_loaded()

    def _on_connect_failed(self, error: QueryError) -> None:
        self._log_message(f"Connection failed: {error.formatted()}")
        warn(self, "Could not connect", error.message)

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

    def _on_connection_lost(self, name: str, message: str) -> None:
        """The session died mid-flight (spec 9).

        Editor tabs and results are left exactly as they are; only the
        connection is gone, and one click brings it back.
        """
        where = f" to {name}" if name else ""
        lost = (
            " The open transaction was not committed." if self._transaction.in_transaction else ""
        )
        self._log_message(f"Connection{where} lost: {message}{lost}")
        self.show_banner(f"Connection{where} lost — {message}{lost}")
        self.statusBar().showMessage("Disconnected", 6000)

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
        was_connected = self._connected
        self._connected = connected
        self._render_transaction()
        if not connected:
            self._set_context(SessionContext())
            if was_connected:
                self.stage_panel.set_connected(False)

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
        self._update_transaction_actions()
        count = len(statements)
        self._log_message(f"Running {count} statement{'' if count == 1 else 's'}…")

    def _on_statement_started(self, index: int, statement, query_id: str) -> None:
        label = f"[{index + 1}] running"
        if query_id:
            label += f" · {query_id}"
            self._last_query_id = query_id
            self._set_status_segment(self.qid_label, query_id)
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
            self._set_status_segment(self.time_label, format_duration(outcome.duration_s))
        if outcome.query_id:
            self._last_query_id = outcome.query_id
            self._set_status_segment(self.qid_label, outcome.query_id)

    def _on_run_finished(self, outcomes: list[StatementOutcome]) -> None:
        self.run_button.setEnabled(self.worker.session.is_connected)
        self.stop_button.setEnabled(False)
        self.cancel_action.setEnabled(False)
        self._update_transaction_actions()
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

    # -- transactions (Q10) --------------------------------------------------

    def _on_transaction_changed(self, state: TransactionState) -> None:
        if state.transaction_id != self._transaction.transaction_id:
            self._transaction_since = datetime.now() if state.in_transaction else None
        self._transaction = state
        self._render_transaction()

    def _render_transaction(self) -> None:
        """Show the session's commit mode, as last read back from it."""
        state = self._transaction
        button = self.commit_button
        self._set_status_visible(button, self._connected)
        if state.in_transaction:
            since = self._transaction_since or datetime.now()
            elapsed = _elapsed_minutes(datetime.now() - since)
            text = f"Transaction open · {elapsed}" if elapsed else "Transaction open"
            button.setIcon(self._transaction_dot)
            mode = "" if state.autocommit is not False else "Auto-commit is off. "
            tip = (
                f"{mode}Transaction {state.transaction_id} has been open since "
                f"{since:%H:%M}. Click to commit or roll it back."
            )
            if not self._transaction_timer.isActive():
                self._transaction_timer.start()
        else:
            self._transaction_timer.stop()
            button.setIcon(QIcon())
            if state.autocommit is None:
                text = "Commit mode ?"
                tip = "SnowDesk could not read AUTOCOMMIT for this session."
            elif state.autocommit:
                text = "Auto-commit"
                tip = "Each statement commits as it runs. Click to change."
            else:
                text = "Manual commit"
                tip = "Changes stay uncommitted until you commit them. Click to change."
        button.setText(f"{text} {MENU_MARK}")
        button.setToolTip(tip)
        self.autocommit_action.setChecked(state.autocommit is True)
        self.manual_commit_action.setChecked(state.autocommit is False)
        self._update_transaction_actions()

    def _update_transaction_actions(self) -> None:
        ready = self._connected and not self.query.is_running
        in_transaction = ready and self._transaction.in_transaction
        self.autocommit_action.setEnabled(ready)
        self.manual_commit_action.setEnabled(ready)
        self.commit_action.setEnabled(in_transaction)
        self.rollback_action.setEnabled(in_transaction)

    def set_autocommit(self, enabled: bool) -> None:
        """Switch the session's commit mode, ending any open transaction first."""
        # The click has already moved the check mark; put it back until the
        # session says the mode really changed.
        self._render_transaction()
        if self._transaction.autocommit is enabled:
            return
        if not self._settle_transaction(
            "The commit mode can only change once the open transaction has ended."
        ):
            return
        self.worker.submit(SetAutocommitJob(enabled=enabled))

    def commit_transaction(self) -> None:
        self.worker.submit(EndTransactionJob(commit=True))
        self.statusBar().showMessage("Committing…")

    def rollback_transaction(self) -> None:
        self.worker.submit(EndTransactionJob(commit=False))
        self.statusBar().showMessage("Rolling back…")

    def _settle_transaction(self, reason: str) -> bool:
        """Ask what to do with an open transaction before ending the session.

        Returns False when the user backs out.  Otherwise the COMMIT or
        ROLLBACK is queued, and runs before whatever the caller queues next.
        """
        if not self._transaction.in_transaction:
            return True
        commit = self.ask_open_transaction(reason)
        if commit is None:
            return False
        self.worker.submit(EndTransactionJob(commit=commit))
        return True

    def ask_open_transaction(self, reason: str) -> bool | None:
        """Commit (True), Roll Back (False), or Cancel (None)."""
        box = message_box(self)
        box.setWindowTitle("Open transaction")
        box.setText("This session has a transaction open.")
        box.setInformativeText(f"{reason} Commit its changes or roll them back?")
        commit = box.addButton("Commit", QMessageBox.ButtonRole.AcceptRole)
        rollback = box.addButton("Roll Back", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(commit)
        box.exec()
        clicked = box.clickedButton()
        if clicked is commit:
            return True
        if clicked is rollback:
            return False
        return None

    def _on_transaction_ended(self, outcome: StatementOutcome) -> None:
        sql = outcome.statement.sql
        if outcome.status is RunStatus.ERROR:
            detail = outcome.error.formatted() if outcome.error else outcome.message
            self._log_message(f"{sql} failed\n{detail}")
            self.statusBar().showMessage(f"{sql} failed — see Messages", 6000)
            self.result_tabs.setCurrentWidget(self.messages)
        else:
            self._log_message(f"{sql}: {outcome.message}")
            self.statusBar().showMessage("Committed" if sql == "COMMIT" else "Rolled back", 4000)
        if outcome.query_id:
            self._last_query_id = outcome.query_id
            self._set_status_segment(self.qid_label, outcome.query_id)
        self.history_panel.reload()

    def _on_autocommit_failed(self, message: str) -> None:
        self._log_message(f"Could not change the commit mode: {message}")
        self.statusBar().showMessage("Commit mode unchanged — see Messages", 6000)

    # -- results -----------------------------------------------------------

    def _on_result_ready(
        self,
        result_id: str,
        columns: list[ColumnInfo],
        rows: list,
        exhausted: bool,
        total: int | None,
        query_id: str = "",
        label: str = "",
    ) -> None:
        model = ResultModel(
            columns=columns,
            first_rows=rows,
            exhausted=exhausted,
            row_cap=self.query.row_cap,
            total=total,
        )
        view = ResultView(
            result_id,
            model,
            self,
            escape_formulas=self._escape_formulas,
            query_id=query_id,
        )
        view.more_requested.connect(self.query.fetch_more)
        view.profile_requested.connect(self.show_query_profile)
        model.cap_reached.connect(
            lambda: self._set_status_segment(self.rows_label, model.status_text())
        )
        view.set_detail_visible(getattr(self, "_show_detail", False))
        self._results[result_id] = view
        if not label:
            self._result_seq += 1
            label = f"Result {self._result_seq}"
        index = self.result_tabs.insertTab(max(0, self.result_tabs.count() - 2), view, label)
        self.result_tabs.setCurrentIndex(index)
        self._set_status_segment(self.rows_label, model.status_text())

    def _on_rows_appended(self, result_id: str, rows: list, exhausted: bool) -> None:
        view = self._results.get(result_id)
        if view is None:
            return
        view.append_rows(rows, exhausted)
        if self.result_tabs.currentWidget() is view:
            self._set_status_segment(self.rows_label, view.model.status_text())

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
        self._result_seq = 0
        self._last_query_id = ""
        self._set_status_segment(self.rows_label, "")
        self._set_status_segment(self.time_label, "")
        self._set_status_segment(self.qid_label, "")

    # -- query profile -----------------------------------------------------

    def target_query_id(self) -> str:
        """The query the profile actions act on.

        The focused result tab first, so profiling the tab you are reading
        works while older tabs are still open; otherwise the last statement
        that ran, which is the only handle left when it produced no grid.
        """
        view = self.result_tabs.currentWidget()
        if isinstance(view, ResultView) and view.query_id:
            return view.query_id
        return self._last_query_id

    def profile_current_query(self) -> None:
        """Open the operator statistics for the focused result.

        The rows Snowflake builds its profile from, not Snowsight's diagram;
        the spec rules out drawing the plan, not reading the numbers.
        """
        query_id = self.target_query_id()
        if not query_id:
            self.statusBar().showMessage("No query to profile yet.", 4000)
            return
        self.show_query_profile(query_id)

    def show_query_profile(self, query_id: str) -> None:
        if not self.worker.session.is_connected:
            self.statusBar().showMessage("Not connected.", 4000)
            return
        self.statusBar().showMessage(f"Reading the profile for {query_id}…", 4000)
        self.worker.submit(ProfileJob(query_id=query_id, page_size=self.query.page_size))

    def copy_current_query_id(self) -> None:
        query_id = self.target_query_id()
        if not query_id:
            self.statusBar().showMessage("No query ID yet.", 4000)
            return
        QApplication.clipboard().setText(query_id)
        self.statusBar().showMessage(f"Copied query ID {query_id}", 3000)

    def _on_profile_empty(self, query_id: str) -> None:
        """Say why a query that clearly ran has no operator statistics."""
        self._log_message(profile.empty_hint(query_id))
        self.result_tabs.setCurrentWidget(self.messages)
        self.statusBar().showMessage("No query profile — see Messages", 6000)

    def _on_profile_failed(self, query_id: str, message: str) -> None:
        self._log_message(f"Could not read the profile for {query_id}: {message}")
        self.result_tabs.setCurrentWidget(self.messages)
        self.statusBar().showMessage("Could not read the query profile", 6000)

    # -- export (R6) -------------------------------------------------------

    def export_current_result(self) -> None:
        """Write the focused result to CSV in full, not just its loaded rows."""
        view = self.result_tabs.currentWidget()
        if not isinstance(view, ResultView):
            self.statusBar().showMessage("Select a result tab to export.", 4000)
            return
        chosen = self.ask_export_path()
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix == "":
            path = path.with_suffix(".csv")

        self._export_dialog = QProgressDialog("Exporting…", "Cancel", 0, 0, self)
        self._export_dialog.setWindowTitle("Export to CSV")
        self._export_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_dialog.setMinimumDuration(0)
        self._export_dialog.canceled.connect(self.worker.cancel_export)
        self._export_dialog.show()

        self.worker.export_csv(
            view.result_id, str(path), self.query.page_size, self._escape_formulas
        )

    def ask_export_path(self) -> str:
        """Where to write the CSV; empty when the user backs out."""
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Export result to CSV", "result.csv", "CSV files (*.csv);;All files (*)"
        )
        return chosen

    def _close_export_dialog(self) -> None:
        dialog = self._export_dialog
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
            self._export_dialog = None

    def _on_export_progress(self, _result_id: str, rows: int) -> None:
        dialog = self._export_dialog
        if dialog is not None:
            dialog.setLabelText(f"Exporting… {rows:,} rows")

    def _on_export_finished(self, _result_id: str, path: str, rows: int) -> None:
        self._close_export_dialog()
        noun = "row" if rows == 1 else "rows"
        self._log_message(f"Exported {rows:,} {noun} to {path}")
        self.statusBar().showMessage(f"Exported {rows:,} {noun}", 6000)

    def _on_export_failed(self, _result_id: str, message: str) -> None:
        self._close_export_dialog()
        self._log_message(f"Export failed: {message}")
        warn(self, "Could not export", message)

    def _on_export_cancelled(self, _result_id: str) -> None:
        self._close_export_dialog()
        self.statusBar().showMessage("Export cancelled", 4000)

    def toggle_cell_detail(self) -> None:
        """Show or hide the detail pane on every result tab (R8)."""
        self._show_detail = not getattr(self, "_show_detail", False)
        self.detail_action.setChecked(self._show_detail)
        for view in self._results.values():
            view.set_detail_visible(self._show_detail)

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

    def _on_browse_failed(self, path: tuple[str, ...], message: str) -> None:
        """The tree row is cut short by the sidebar; Messages has it in full."""
        where = qualify(*path) if path else "databases"
        self._log_message(f"Could not load {where}: {message}")

    def _on_sidebar_changed(self, index: int) -> None:
        QSettings().setValue(SIDEBAR_KEY, index)
        if self.sidebar.widget(index) is self.stage_panel:
            self.stage_panel.ensure_loaded()

    def show_table_stage(self, path: tuple[str, ...]) -> None:
        """Open a table's own stage in the Stages tab (ST12)."""
        database, schema, table = path[0], path[1], path[2]
        # Opened before the tab is switched to, so switching does not start a
        # second load of the stage list behind this one.
        self.stage_panel.open_table_stage(database, schema, table)
        self.sidebar.setCurrentWidget(self.stage_panel)

    def _close_current_tab(self) -> None:
        self.editors.close_tab(self.editors.currentIndex())

    def _on_file_changed(self, path: object) -> None:
        """Title the window the way macOS titles a document window.

        The file name alone; the full path belongs to the proxy icon, which
        setWindowFilePath provides.
        """
        if path:
            self.setWindowTitle(Path(str(path)).name)
            self.setWindowFilePath(str(path))
        else:
            self.setWindowTitle("SnowDesk")
            self.setWindowFilePath("")

    def _clear_history(self) -> None:
        if self.confirm_clear_history():
            self.history.clear()
            self.history_panel.reload()

    def confirm_clear_history(self) -> bool:
        """Confirm before destroying history, defaulting to keeping it.

        Qt's question() makes Yes the default, so Return would wipe the
        history; the platform convention is the safe choice by default and a
        distinct destructive button.
        """
        box = message_box(self)
        box.setText("Delete all query history?")
        box.setInformativeText(
            "Every statement SnowDesk has recorded on this Mac will be removed. "
            "You can't undo this action."
        )
        delete = box.addButton("Delete History", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is delete

    def ask_quit_during_transfer(self) -> bool:
        """Stop the running transfer and quit (True), or keep running (False)."""
        box = message_box(self)
        box.setWindowTitle("Transfer in progress")
        box.setText("A stage transfer is still running.")
        box.setInformativeText(
            "Quitting stops it once the file being transferred now is done. "
            "Files not yet started are left where they are."
        )
        quit_button = box.addButton("Stop and Quit", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep Running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is quit_button

    def _on_worker_error(self, message: str) -> None:
        self._log_message(f"Internal error: {message}")
        self.statusBar().showMessage("An internal error occurred — see Messages", 6000)

    def _log_message(self, text: str) -> None:
        self.messages.appendPlainText(text)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Tab contents are autosaved, so quitting only has to ask about
        # changes that live in Snowflake: an open transaction (E2, Q10).  The
        # COMMIT or ROLLBACK is queued ahead of the worker's shutdown job.
        if self.stages.is_busy and not self.ask_quit_during_transfer():
            event.ignore()
            return
        if not self._settle_transaction("Quitting ends this session and its open transaction."):
            event.ignore()
            return
        self.stages.stop()
        self.editors.save_session()
        self.closing.emit()
        super().closeEvent(event)


def _elapsed_minutes(delta: timedelta) -> str:
    """``4m`` or ``1h 12m``; empty under a minute, where a count is noise."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"
