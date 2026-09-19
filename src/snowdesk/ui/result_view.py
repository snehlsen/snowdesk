"""Results grid: incremental model, two-line headers, copy as TSV (R1-R5)."""

from __future__ import annotations

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
    QStyle,
    QStyleOptionHeader,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from snowdesk.config import DEFAULT_ROW_CAP
from snowdesk.model import ColumnInfo
from snowdesk.util.export import rows_to_tsv
from snowdesk.util.formatting import NULL_TEXT, format_cell, format_row_count

MAX_COLUMN_WIDTH = 420
NULL_COLOR = QColor("#8a8f98")


class ResultModel(QAbstractTableModel):
    """Table model backed by pages fetched on demand (R2).

    The model never fetches itself: ``fetchMore`` only signals intent, and the
    controller schedules the work on the worker thread (spec 7.5).
    """

    more_requested = Signal()
    cap_reached = Signal()

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

    def __init__(
        self,
        result_id: str,
        model: ResultModel,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.result_id = result_id
        self.model = model

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
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        self.footer = QLabel("", self)
        self.footer.setVisible(False)
        self.footer.setStyleSheet("color: #a15c00; padding: 4px 8px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.table)
        layout.addWidget(self.footer)

        model.more_requested.connect(lambda: self.more_requested.emit(self.result_id))
        model.cap_reached.connect(self._on_cap_reached)

        self._add_shortcuts()
        self._size_columns()

    # -- actions -----------------------------------------------------------

    def _add_shortcuts(self) -> None:
        copy = QAction("Copy", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self.copy_selection)
        copy_headers = QAction("Copy with Headers", self)
        copy_headers.setShortcut(QKeySequence("Ctrl+Shift+C"))
        copy_headers.triggered.connect(lambda: self.copy_selection(with_headers=True))
        for action in (copy, copy_headers):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._copy_action = copy
        self._copy_headers_action = copy_headers

    def _show_context_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        menu.addAction(self._copy_action)
        menu.addAction(self._copy_headers_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def selected_tsv(self, with_headers: bool = False) -> str:
        """Selected cells as TSV; the whole visible result when nothing is selected."""
        indexes = (
            self.table.selectionModel().selectedIndexes() if self.table.selectionModel() else []
        )
        if not indexes:
            return rows_to_tsv(self.model.rows, self.model.columns, with_headers=with_headers)
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
        subset = [self.model.rows[r] for r in rows]
        return rows_to_tsv(
            subset, self.model.columns, with_headers=with_headers, column_indexes=cols
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
        self.footer.setText(
            f"Row cap reached — {self.model.rowCount():,} rows loaded. "
            "Export to CSV to get the full result."
        )
        self.footer.setVisible(True)

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


def null_display_text() -> str:
    return NULL_TEXT
