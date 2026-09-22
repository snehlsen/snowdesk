"""History panel over the SQLite store (H1/H2)."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
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
    """Searchable list of executed statements.

    It is also where a query id outlives its result tab, which the next run
    closes -- so profiling a query you ran a while ago starts here.
    """

    profile_requested = Signal(str)  # query id
    status_message = Signal(str)

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
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

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

    # -- per-row actions ---------------------------------------------------

    def entry_at(self, row: int) -> HistoryEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def _show_context_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        entry = self.entry_at(index.row()) if index.isValid() else None
        if entry is None:
            return
        menu = QMenu(self)
        copy_sql = menu.addAction("Copy Statement")
        menu.addSeparator()
        copy_qid = menu.addAction("Copy Query ID")
        profile = menu.addAction("Query Profile")
        # A cancelled statement, or one that never reached Snowflake, has no id.
        for act in (copy_qid, profile):
            act.setEnabled(bool(entry.query_id))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is copy_sql:
            QGuiApplication.clipboard().setText(entry.sql)
            self.status_message.emit("Copied statement")
        elif chosen is copy_qid and entry.query_id:
            QGuiApplication.clipboard().setText(entry.query_id)
            self.status_message.emit(f"Copied query ID {entry.query_id}")
        elif chosen is profile and entry.query_id:
            self.profile_requested.emit(entry.query_id)
