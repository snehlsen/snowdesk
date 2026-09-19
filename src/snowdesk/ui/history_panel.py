"""History panel over the SQLite store (H1/H2)."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from snowdesk.storage.history import HistoryEntry, HistoryStore
from snowdesk.util.formatting import format_duration

_COLUMNS = ["When", "Status", "Duration", "Rows", "Statement"]


class HistoryPanel(QWidget):
    """Searchable list of executed statements; double-click loads one."""

    statement_chosen = Signal(str)

    def __init__(self, store: HistoryStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search history…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)

        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(lambda: self.reload())

        self.table = QTableWidget(0, len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        top = QHBoxLayout()
        top.setContentsMargins(6, 6, 6, 0)
        top.addWidget(self.search, 1)
        top.addWidget(refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.table)

        self._entries: list[HistoryEntry] = []
        self.reload()

    def reload(self, _term: str | None = None) -> None:
        self._entries = self.store.search(self.search.text())
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            when = dt.datetime.fromtimestamp(entry.ts).strftime("%Y-%m-%d %H:%M:%S")
            values = [
                when,
                entry.status,
                format_duration(entry.duration_s),
                "" if entry.row_count is None else f"{entry.row_count:,}",
                entry.first_line,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(entry.sql if col == len(values) - 1 else text)
                if col in (2, 3):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)

    def _on_double_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._entries):
            self.statement_chosen.emit(self._entries[row].sql)
