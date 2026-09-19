"""Lazy database → schema → table/view tree (B1)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem, QWidget

from snowdesk.controllers.browser import BrowserController
from snowdesk.db import browser as browse
from snowdesk.db.identifiers import qualify
from snowdesk.model import ObjectNode

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
LOADED_ROLE = Qt.ItemDataRole.UserRole + 3

_EXPANDABLE = {browse.DATABASE, browse.SCHEMA, browse.TABLE, browse.VIEW}


class ObjectTree(QTreeWidget):
    """Each level is fetched with a single ``SHOW`` on first expand."""

    insert_requested = Signal(str)  # fully qualified, quoted name

    def __init__(self, controller: BrowserController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setHeaderLabels(["Object", "Type"])
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setExpandsOnDoubleClick(False)

        self.itemExpanded.connect(self._on_expanded)
        self.itemDoubleClicked.connect(self._on_double_clicked)
        controller.nodes_ready.connect(self._on_nodes_ready)
        controller.failed.connect(self._on_failed)

    # -- population --------------------------------------------------------

    def load_roots(self) -> None:
        self.clear()
        placeholder = QTreeWidgetItem(self, ["Loading…", ""])
        placeholder.setDisabled(True)
        self.controller.load(())

    def refresh(self, item: QTreeWidgetItem | None = None) -> None:
        """Re-run the SHOW behind ``item`` (or the roots) (B5)."""
        if item is None:
            self.controller.load((), refresh=True)
            return
        path = self._path_of(item)
        item.setData(0, LOADED_ROLE, False)
        item.takeChildren()
        self.controller.load(path, refresh=True)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, LOADED_ROLE):
            return
        path = self._path_of(item)
        self._set_placeholder(item, "Loading…")
        self.controller.load(path)

    def _on_nodes_ready(self, path: tuple[str, ...], nodes: list[ObjectNode]) -> None:
        parent = self._item_for_path(path)
        if path and parent is None:
            return  # the node was collapsed away before the answer arrived
        self._replace_children(parent, nodes)
        if parent is not None:
            parent.setData(0, LOADED_ROLE, True)

    def _on_failed(self, path: tuple[str, ...], message: str) -> None:
        parent = self._item_for_path(path)
        self._set_placeholder(parent, message or "Could not load", error=True)

    def _replace_children(self, parent: QTreeWidgetItem | None, nodes: list[ObjectNode]) -> None:
        if parent is None:
            self.clear()
        else:
            parent.takeChildren()
        if not nodes:
            self._set_placeholder(parent, "empty")
            return
        for node in nodes:
            item = QTreeWidgetItem([node.name, node.detail or node.kind])
            item.setData(0, PATH_ROLE, node.path)
            item.setData(0, KIND_ROLE, node.kind)
            item.setData(0, LOADED_ROLE, False)
            if node.kind in _EXPANDABLE:
                # A dummy child makes the expander arrow appear before we know
                # whether there is anything below.
                item.addChild(QTreeWidgetItem(["Loading…", ""]))
            if node.kind == browse.COLUMN:
                item.setForeground(1, QColor("#8a8f98"))
            if parent is None:
                self.addTopLevelItem(item)
            else:
                parent.addChild(item)

    def _set_placeholder(
        self, parent: QTreeWidgetItem | None, text: str, error: bool = False
    ) -> None:
        item = QTreeWidgetItem([text, ""])
        item.setDisabled(True)
        if error:
            item.setForeground(0, QColor("#e5534b"))
            font = QFont()
            font.setItalic(True)
            item.setFont(0, font)
        if parent is None:
            self.clear()
            self.addTopLevelItem(item)
        else:
            parent.takeChildren()
            parent.addChild(item)

    # -- lookups -----------------------------------------------------------

    def _path_of(self, item: QTreeWidgetItem) -> tuple[str, ...]:
        path = item.data(0, PATH_ROLE)
        return tuple(path) if path else ()

    def _item_for_path(self, path: tuple[str, ...]) -> QTreeWidgetItem | None:
        """Find the item whose stored path equals ``path``, walking one level
        at a time so collapsed or replaced branches simply miss."""
        if not path:
            return None
        item: QTreeWidgetItem | None = None
        for depth in range(len(path)):
            prefix = path[: depth + 1]
            if item is None:
                candidates = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
            else:
                candidates = [item.child(i) for i in range(item.childCount())]
            item = next(
                (c for c in candidates if c is not None and self._path_of(c) == prefix), None
            )
            if item is None:
                return None
        return item

    # -- interaction -------------------------------------------------------

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        path = self._path_of(item)
        kind = item.data(0, KIND_ROLE)
        if not path:
            return
        if kind == browse.COLUMN:
            self.insert_requested.emit(qualify(path[-1]))
            return
        self.insert_requested.emit(qualify(*path))

    def selected_path(self) -> tuple[str, ...]:
        item = self.currentItem()
        return self._path_of(item) if item else ()

    def filter_tree(self, term: str) -> None:
        """Hide nodes whose name does not contain ``term`` (B5)."""
        needle = term.strip().lower()
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item is not None:
                _apply_filter(item, needle)


def _apply_filter(item: QTreeWidgetItem, needle: str) -> bool:
    """Show ``item`` if it or any descendant matches; returns whether it is visible."""
    visible = not needle or needle in item.text(0).lower()
    for i in range(item.childCount()):
        child = item.child(i)
        if child is not None and _apply_filter(child, needle):
            visible = True
    item.setHidden(not visible)
    return visible
