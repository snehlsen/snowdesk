"""Results grid: incremental model, two-line headers, copy as TSV (R1-R5)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QStyle,
    QStyleOptionHeader,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from snowdesk.config import DEFAULT_ROW_CAP
from snowdesk.model import ColumnInfo
from snowdesk.util.export import rows_to_tsv
from snowdesk.util.formatting import NULL_TEXT, format_cell, format_row_count, pretty_json

MAX_COLUMN_WIDTH = 420
NULL_COLOR = QColor("#8a8f98")


class ResultModel(QAbstractTableModel):
    """Table model backed by pages fetched on demand (R2).

    The model never fetches itself: ``fetchMore`` only signals intent, and the
    controller schedules the work on the worker thread (spec 7.5).
    """

    more_requested = Signal()
    cap_reached = Signal()
    sorted_changed = Signal()

    def __init__(
        self,
        columns: list[ColumnInfo],
        first_rows: list[tuple[Any, ...]],
        exhausted: bool,
        row_cap: int = DEFAULT_ROW_CAP,
        total: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[tuple[Any, ...]] = list(first_rows)
        self._exhausted = exhausted
        self._loading = False
        self._row_cap = row_cap
        self._total = total
        self._capped = False
        self._sorted_column: int | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder

    # -- introspection -----------------------------------------------------

    @property
    def columns(self) -> list[ColumnInfo]:
        return self._columns

    @property
    def rows(self) -> list[tuple[Any, ...]]:
        return self._rows

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def capped(self) -> bool:
        return self._capped

    def status_text(self) -> str:
        text = format_row_count(len(self._rows), self._total, self._exhausted)
        if self._capped:
            text += f" (capped at {self._row_cap:,})"
        return text

    # -- QAbstractTableModel ----------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        value = self._rows[index.row()][index.column()]
        column = self._columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return format_cell(value, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            text = format_cell(value, column)
            return text if len(text) > 60 else None
        if role == Qt.ItemDataRole.ForegroundRole and value is None:
            return NULL_COLOR  # R3: NULL is visually distinct from ''
        if role == Qt.ItemDataRole.FontRole and value is None:
            font = QFont()
            font.setItalic(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and column.is_numeric:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.UserRole:
            return value
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Vertical:
            if role == Qt.ItemDataRole.DisplayRole:
                return str(section + 1)
            return None
        if section >= len(self._columns):
            return None
        column = self._columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.name
        if role == Qt.ItemDataRole.UserRole:
            return column.type_hint
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{column.name} — {column.type_hint}"
        return None

    def canFetchMore(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> bool:
        return (
            not parent.isValid()
            and not self._exhausted
            and not self._loading
            and len(self._rows) < self._row_cap
        )

    def fetchMore(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> None:
        if parent.isValid() or self._loading:
            return
        self._loading = True
        self.more_requested.emit()

    # -- page arrival ------------------------------------------------------

    def append_rows(self, rows: list[tuple[Any, ...]], exhausted: bool) -> None:
        self._loading = False
        self._exhausted = exhausted
        if not rows:
            return
        room = self._row_cap - len(self._rows)
        if room <= 0:
            self._mark_capped()
            return
        if len(rows) > room:
            rows = rows[:room]
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()
        if len(self._rows) >= self._row_cap and not self._exhausted:
            self._mark_capped()

    def _mark_capped(self) -> None:
        if self._capped:
            return
        self._capped = True
        self.cap_reached.emit()

    @property
    def sorted_column(self) -> int | None:
        return self._sorted_column

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort the rows held in memory (R7).

        Client-side: it orders what has been fetched, not the whole result.
        The view says so, because a sorted partial result otherwise looks like
        an ordered answer to a question nobody asked the database.
        """
        if not 0 <= column < len(self._columns):
            return
        reverse = order == Qt.SortOrder.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=lambda row: _sort_key(row[column]), reverse=reverse)
        self._sorted_column = column
        self._sort_order = order
        self.layoutChanged.emit()
        self.sorted_changed.emit()

    def mark_exhausted(self) -> None:
        self._loading = False
        self._exhausted = True


class ResultHeaderView(QHeaderView):
    """Two-line header: column name above its type hint (R1).

    Qt's default section painter draws a single centred label, so the section
    is painted with an empty label and both lines are drawn here.
    """

    _PADDING = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(False)
        self.setHighlightSections(False)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def _type_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.5))
        return font

    def sectionSizeFromContents(self, index: int) -> QSize:
        """Size a section from both header lines, not just the name."""
        model = self.model()
        if model is None:
            return super().sectionSizeFromContents(index)
        name = str(model.headerData(index, self.orientation(), Qt.ItemDataRole.DisplayRole) or "")
        type_hint = str(model.headerData(index, self.orientation(), Qt.ItemDataRole.UserRole) or "")
        name_font = QFont(self.font())
        name_font.setBold(True)
        width = max(
            QFontMetrics(name_font).horizontalAdvance(name),
            QFontMetrics(self._type_font()).horizontalAdvance(type_hint),
        )
        line = self.fontMetrics().height()
        return QSize(width + self._PADDING, line * 2 + 10)

    def paintSection(self, painter: QPainter, rect: QRect, index: int) -> None:
        model = self.model()
        if model is None:
            return
        self._paint_background(painter, rect, index)

        name = model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        type_hint = model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole)
        if name is None:
            return
        line = self.fontMetrics().height()
        text_rect = rect.adjusted(6, 3, -6, -3)

        painter.save()
        name_font = QFont(self.font())
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.drawText(
            QRect(text_rect.left(), text_rect.top(), text_rect.width(), line),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(name_font).elidedText(
                str(name), Qt.TextElideMode.ElideRight, text_rect.width()
            ),
        )
        painter.restore()

        if type_hint:
            painter.save()
            painter.setPen(NULL_COLOR)
            type_font = self._type_font()
            painter.setFont(type_font)
            painter.drawText(
                QRect(text_rect.left(), text_rect.top() + line, text_rect.width(), line),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(type_font).elidedText(
                    str(type_hint), Qt.TextElideMode.ElideRight, text_rect.width()
                ),
            )
            painter.restore()

    def _paint_background(self, painter: QPainter, rect: QRect, index: int) -> None:
        """Draw the section frame with no label, so our own text is the only text."""
        option = QStyleOptionHeader()
        try:
            self.initStyleOptionForIndex(option, index)
        except (AttributeError, TypeError):  # pragma: no cover - older bindings
            painter.fillRect(rect, self.palette().button())
            return
        option.rect = rect
        option.text = ""
        option.icon = QIcon()
        self.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, self)


class ResultView(QWidget):
    """A result tab: the grid plus its footer notice."""

    more_requested = Signal(str)  # result id
    profile_requested = Signal(str)  # query id

    def __init__(
        self,
        result_id: str,
        model: ResultModel,
        parent: QWidget | None = None,
        escape_formulas: bool = True,
        query_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.result_id = result_id
        self.model = model
        self._escape_formulas = escape_formulas
        #: The query that produced these rows, kept because it is the only
        #: reliable way back to this result's profile: the session's last query
        #: is the connector's RESULT_SCAN, not this one (see snowdesk.db.profile).
        self.query_id = query_id or ""

        self.table = QTableView(self)
        self.table.setModel(model)
        self.table.setHorizontalHeader(ResultHeaderView(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(True)
        # Uniform row heights keep very wide results scrolling smoothly.
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        # Enabling sorting makes Qt sort by the indicator's column straight
        # away, which would reorder the result before the user asked for
        # anything.  Clearing the indicator first leaves the rows in the order
        # Snowflake returned them until a header is clicked.
        header = self.table.horizontalHeader()
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        self.footer = QLabel("", self)
        self.footer.setVisible(False)
        self.footer.setStyleSheet("padding: 4px 8px;")

        # A query that legitimately returns nothing should say so rather than
        # leaving an empty panel that looks like a failure.
        self.empty_label = QLabel("No rows returned", self.table)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setEnabled(False)
        self.empty_label.setVisible(False)

        # A cell can hold a paragraph or a nested JSON document; the grid
        # shows one line of it, and this shows the rest (R8).
        self.detail = QPlainTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.detail.setPlaceholderText("Select a cell to see its full value.")
        self.detail.setVisible(False)

        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.splitter)
        layout.addWidget(self.footer)

        model.more_requested.connect(lambda: self.more_requested.emit(self.result_id))
        model.cap_reached.connect(self._on_cap_reached)
        model.sorted_changed.connect(self._refresh_footer)
        model.modelReset.connect(self._refresh_empty_state)
        model.rowsInserted.connect(self._refresh_empty_state)
        self._refresh_empty_state()

        selection = self.table.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(lambda *_: self._refresh_detail())

        self._add_shortcuts()
        self._size_columns()

    # -- actions -----------------------------------------------------------

    def _add_shortcuts(self) -> None:
        copy = QAction("Copy", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(lambda: self.copy_selection())
        copy_headers = QAction("Copy with Headers", self)
        copy_headers.setShortcut(QKeySequence("Ctrl+Shift+C"))
        copy_headers.triggered.connect(lambda: self.copy_selection(with_headers=True))
        for action in (copy, copy_headers):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._copy_action = copy
        self._copy_headers_action = copy_headers

        # No shortcuts of their own: the window carries those, so pressing one
        # over the grid is not an ambiguous overload.
        self._copy_qid_action = QAction("Copy Query ID", self)
        self._copy_qid_action.triggered.connect(lambda: self.copy_query_id())
        self._profile_action = QAction("Query Profile", self)
        self._profile_action.triggered.connect(lambda: self.request_profile())
        self._refresh_query_actions()

    def _show_context_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        menu.addAction(self._copy_action)
        menu.addAction(self._copy_headers_action)
        menu.addSeparator()
        menu.addAction(self._copy_qid_action)
        menu.addAction(self._profile_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # -- the query behind the grid ----------------------------------------

    def set_query_id(self, query_id: str) -> None:
        self.query_id = query_id or ""
        self._refresh_query_actions()

    def _refresh_query_actions(self) -> None:
        for action in (self._copy_qid_action, self._profile_action):
            action.setEnabled(bool(self.query_id))

    def copy_query_id(self) -> bool:
        """Put this result's query id on the clipboard; False if there is none."""
        if not self.query_id:
            return False
        QGuiApplication.clipboard().setText(self.query_id)
        return True

    def request_profile(self) -> bool:
        if not self.query_id:
            return False
        self.profile_requested.emit(self.query_id)
        return True

    # -- cell detail (R8) --------------------------------------------------

    def set_escape_formulas(self, escape: bool) -> None:
        """Follow the preference for copies out of this grid."""
        self._escape_formulas = escape

    def set_detail_visible(self, visible: bool) -> None:
        self.detail.setVisible(visible)
        if visible:
            self._refresh_detail()

    def detail_is_visible(self) -> bool:
        return self.detail.isVisibleTo(self)

    def _refresh_detail(self) -> None:
        if not self.detail.isVisibleTo(self):
            return
        self.detail.setPlainText(self.current_cell_text())

    def current_cell_text(self) -> str:
        """The focused cell in full: JSON pretty-printed, everything else raw.

        Raw on purpose: this panel is for reading the value, not for handing it
        to a spreadsheet, so it shows exactly what the database returned.
        """
        index = self.table.currentIndex()
        if not index.isValid():
            return ""
        value = self.model.data(index, Qt.ItemDataRole.UserRole)
        if value is None:
            return NULL_TEXT
        column = self.model.columns[index.column()]
        if column.is_json:
            return pretty_json(value)
        return format_cell(value, column)

    def selected_tsv(self, with_headers: bool = False) -> str:
        """Selected cells as TSV; the whole visible result when nothing is selected."""
        indexes = (
            self.table.selectionModel().selectedIndexes() if self.table.selectionModel() else []
        )
        if not indexes:
            return rows_to_tsv(
                self.model.rows,
                self.model.columns,
                with_headers=with_headers,
                escape_formulas=self._escape_formulas,
            )
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
        subset = [self.model.rows[r] for r in rows]
        return rows_to_tsv(
            subset,
            self.model.columns,
            with_headers=with_headers,
            column_indexes=cols,
            escape_formulas=self._escape_formulas,
        )

    def copy_selection(self, with_headers: bool = False) -> None:
        """Copy selected cells as TSV (R5)."""
        QGuiApplication.clipboard().setText(self.selected_tsv(with_headers=with_headers))

    # -- incremental arrival ----------------------------------------------

    def append_rows(self, rows: list[tuple[Any, ...]], exhausted: bool) -> None:
        was_empty = self.model.rowCount() == 0
        self.model.append_rows(rows, exhausted)
        if was_empty and rows:
            self._size_columns()

    def _on_cap_reached(self) -> None:
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        """Say what the grid is not showing, when it is not showing all of it."""
        notes: list[str] = []
        if self.model.capped:
            notes.append(f"Row cap reached — {self.model.rowCount():,} rows loaded.")
        if self.model.sorted_column is not None and not self.model.exhausted:
            notes.append("Sorted over the rows loaded so far, not the whole result.")
        if notes:
            notes.append("Export to CSV (⌘E) for the full result.")
            self.footer.setText(" ".join(notes))
        self.footer.setVisible(bool(notes))

    def _refresh_empty_state(self, *_args: object) -> None:
        empty = self.model.rowCount() == 0 and self.model.exhausted
        self.empty_label.setVisible(empty)
        if empty:
            self.empty_label.setGeometry(self.table.viewport().geometry())

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._refresh_empty_state()

    def _size_columns(self) -> None:
        """Size columns from the loaded rows once, then leave them to the user.

        Recomputing on every page would stutter on wide results, so widths are
        derived from the first page and capped.
        """
        header = self.table.horizontalHeader()
        self.table.resizeColumnsToContents()
        for i in range(self.model.columnCount()):
            if self.table.columnWidth(i) > MAX_COLUMN_WIDTH:
                self.table.setColumnWidth(i, MAX_COLUMN_WIDTH)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)


def _sort_key(value: Any) -> tuple[int, Any]:
    """Order values of mixed types without comparing across them.

    A column can hold NULLs beside numbers, or strings beside dates.  NULLs
    sort last in ascending order, and anything not directly comparable falls
    back to its rendered text rather than raising.
    """
    if value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float, Decimal)):
        return (0, value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return (1, value.isoformat())
    return (1, str(value))


def null_display_text() -> str:
    return NULL_TEXT
