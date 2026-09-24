"""The Stages sidebar tab: stages, their folders and files, and transfers.

See docs/stage-browser.md.  The tree is databases → schemas → stages, from
one ``SHOW STAGES IN ACCOUNT``, with the user stage first.  A stage is listed
once, on first expand, and its folders are built from that listing on the
client and filled in as they are opened.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from snowdesk.controllers.stages import StageController
from snowdesk.db import stages as stage_ops
from snowdesk.model import (
    FileResult,
    FileStatus,
    StageFile,
    StageKind,
    StageRef,
    TransferKind,
    TransferPlan,
    TransferProgress,
    TransferSummary,
)
from snowdesk.ui.dialogs import message_box
from snowdesk.ui.object_tree import apply_filter
from snowdesk.util.formatting import format_bytes

KIND_ROLE = Qt.ItemDataRole.UserRole + 1
STAGE_ROLE = Qt.ItemDataRole.UserRole + 2
#: Stage-relative path: ``""`` for a stage, ``a/b/`` for a folder, ``a/b/c.csv``.
PATH_ROLE = Qt.ItemDataRole.UserRole + 3
#: A folder's contents from the listing, until it is opened; a file's StageFile.
DATA_ROLE = Qt.ItemDataRole.UserRole + 4
LOADED_ROLE = Qt.ItemDataRole.UserRole + 5

DATABASE = "database"
SCHEMA = "schema"
STAGE = "stage"
FOLDER = "folder"
FILE = "file"

_ICONS = {
    DATABASE: QStyle.StandardPixmap.SP_DriveHDIcon,
    SCHEMA: QStyle.StandardPixmap.SP_DirIcon,
    STAGE: QStyle.StandardPixmap.SP_DriveNetIcon,
    FOLDER: QStyle.StandardPixmap.SP_DirIcon,
    FILE: QStyle.StandardPixmap.SP_FileIcon,
}

#: How many conflicting names the Replace prompt spells out.
CONFLICTS_SHOWN = 8

_VERBS = {
    TransferKind.UPLOAD: "Uploading",
    TransferKind.DOWNLOAD: "Downloading",
    TransferKind.REMOVE: "Deleting",
}


def _kind_label(stage: StageRef) -> str:
    """Only the exception is labelled: an internal stage is the ordinary case,
    and the user and table stages already say what they are in their names."""
    return "" if stage.internal else "External"


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


class StageTree(QTreeWidget):
    """The tree itself: population, lookups, and Finder drops."""

    #: Files dropped from Finder: stage, stage folder, local paths.
    files_dropped = Signal(object, str, object)

    def __init__(self, controller: StageController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setHeaderLabels(["Name", "Size"])
        # Sizes stay whole and names give way, shortened in the middle as
        # Finder does: in a narrow sidebar the size is what gets cut
        # otherwise, and "orders_0…csv.gz" still says which file it is.
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(True)
        #: Table stages opened from the Objects tree, kept across a refresh.
        self._table_stages: list[StageRef] = []
        #: A stage to select and open once the stage list arrives.
        self._focus: StageRef | None = None
        self.itemExpanded.connect(self._on_expanded)
        controller.listed.connect(self._on_listed)
        controller.list_failed.connect(self._on_list_failed)

    # -- population --------------------------------------------------------

    def show_message(self, text: str, *, error: bool = False) -> None:
        self.clear()
        self.addTopLevelItem(self._notice(text, error=error))

    def populate(self, stages: list[StageRef]) -> None:
        """The user stage, then every stage grouped by database and schema."""
        self.clear()
        self._stage_item(StageRef(kind=StageKind.USER), None)
        for stage in [*stages, *self._table_stages]:
            self._stage_item(stage, self._schema_item(stage.database, stage.schema))
        if self._focus is not None:
            item = self.stage_item(self._focus)
            self._focus = None
            if item is not None:
                self.reveal(item)

    def add_table_stage(self, stage: StageRef, *, when_listed: bool = False) -> None:
        """Add ``@%table`` under its schema and open it.

        With ``when_listed``, the stage list is on its way and would replace
        the item straight away, so it is added and opened once it arrives.
        """
        if stage not in self._table_stages:
            self._table_stages.append(stage)
        if when_listed:
            self._focus = stage
            return
        item = self.stage_item(stage) or self._stage_item(
            stage, self._schema_item(stage.database, stage.schema)
        )
        self.reveal(item)

    def reveal(self, item: QTreeWidgetItem) -> None:
        """Select, open and scroll to ``item``."""
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.setCurrentItem(item)
        item.setExpanded(True)
        self.scrollToItem(item)

    def _schema_item(self, database: str, schema: str) -> QTreeWidgetItem:
        db = self._child_named(None, database, DATABASE)
        return self._child_named(db, schema, SCHEMA)

    def _child_named(self, parent: QTreeWidgetItem | None, name: str, kind: str) -> QTreeWidgetItem:
        count = self.topLevelItemCount() if parent is None else parent.childCount()
        for i in range(count):
            item = self.topLevelItem(i) if parent is None else parent.child(i)
            if item is not None and item.data(0, KIND_ROLE) == kind and item.text(0) == name:
                return item
        item = self._item(name, "", kind)
        item.setData(0, LOADED_ROLE, True)
        self._insert_sorted(parent, item)
        return item

    def _insert_sorted(self, parent: QTreeWidgetItem | None, item: QTreeWidgetItem) -> None:
        """Keep groups in name order; the user stage stays first at the top."""
        count = self.topLevelItemCount() if parent is None else parent.childCount()
        index = count
        for i in range(count):
            sibling = self.topLevelItem(i) if parent is None else parent.child(i)
            if sibling is None:
                continue
            stage = sibling.data(0, STAGE_ROLE)
            if stage is not None and stage.kind is StageKind.USER:
                continue
            if sibling.text(0).lower() > item.text(0).lower():
                index = i
                break
        if parent is None:
            self.insertTopLevelItem(index, item)
        else:
            parent.insertChild(index, item)

    def _stage_item(self, stage: StageRef, parent: QTreeWidgetItem | None) -> QTreeWidgetItem:
        label = "@~" if stage.kind is StageKind.USER else stage.name
        if stage.kind is StageKind.TABLE:
            label = f"@%{stage.name}"
        item = self._item(label, _kind_label(stage), STAGE)
        item.setData(0, STAGE_ROLE, stage)
        item.setData(0, PATH_ROLE, "")
        item.setData(0, LOADED_ROLE, False)
        item.setToolTip(0, stage_ops.stage_name(stage) + (f"\n{stage.url}" if stage.url else ""))
        if not stage.internal:
            item.setForeground(1, QColor("#8a8f98"))
        item.addChild(self._notice("Loading…"))
        if parent is None:
            self.addTopLevelItem(item)
        else:
            self._insert_sorted(parent, item)
        return item

    def _item(self, name: str, detail: str, kind: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name, detail])
        item.setData(0, KIND_ROLE, kind)
        pixmap = _ICONS.get(kind)
        if pixmap is not None:
            item.setIcon(0, self.style().standardIcon(pixmap))
        return item

    def _notice(self, text: str, *, error: bool = False) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text, ""])
        item.setDisabled(True)
        font = QFont()
        font.setItalic(True)
        item.setFont(0, font)
        if error:
            item.setForeground(0, QColor("#e5534b"))
        return item

    # -- listing -----------------------------------------------------------

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, LOADED_ROLE):
            return
        kind = item.data(0, KIND_ROLE)
        if kind == STAGE:
            item.takeChildren()
            item.addChild(self._notice("Loading…"))
            self.controller.list_stage(item.data(0, STAGE_ROLE))
        elif kind == FOLDER:
            folder = item.data(0, DATA_ROLE)
            if folder is not None:
                self._fill(item, item.data(0, STAGE_ROLE), folder, truncated=False)

    def refresh(self, item: QTreeWidgetItem) -> None:
        """Re-list a stage or folder from the server."""
        kind = item.data(0, KIND_ROLE)
        if kind not in (STAGE, FOLDER):
            return
        item.setData(0, LOADED_ROLE, False)
        item.takeChildren()
        item.addChild(self._notice("Loading…"))
        item.setExpanded(True)
        self.controller.list_stage(item.data(0, STAGE_ROLE), item.data(0, PATH_ROLE))

    def refresh_stage(self, stage: StageRef) -> None:
        """Re-list a stage after a transfer, if it has been opened at all."""
        item = self.stage_item(stage)
        if item is not None and item.data(0, LOADED_ROLE):
            self.refresh(item)

    def _on_listed(
        self, stage: StageRef, prefix: str, files: list[StageFile], truncated: bool
    ) -> None:
        item = self._item_for(stage, prefix)
        if item is None:
            return  # collapsed or refreshed away before the answer came
        self._fill(item, stage, stage_ops.build_tree(files, prefix), truncated=truncated)
        if truncated:
            count = len(files)
            item.setToolTip(1, f"Listing stopped at {count:,} files")

    def _on_list_failed(self, stage: StageRef, prefix: str, message: str) -> None:
        item = self._item_for(stage, prefix)
        if item is None:
            return
        item.takeChildren()
        item.addChild(self._notice(message or "Could not list", error=True))

    def _fill(
        self,
        parent: QTreeWidgetItem,
        stage: StageRef,
        folder: stage_ops.Folder,
        *,
        truncated: bool,
    ) -> None:
        parent.takeChildren()
        parent.setData(0, LOADED_ROLE, True)
        parent.setData(0, DATA_ROLE, None)
        for sub in folder.folders.values():
            child = self._item(f"{sub.name}/", _plural(sub.file_count, "file"), FOLDER)
            child.setData(0, STAGE_ROLE, stage)
            child.setData(0, PATH_ROLE, sub.path)
            # Filled in when opened: a stage with a hundred thousand files
            # should not build a hundred thousand rows up front.
            child.setData(0, DATA_ROLE, sub)
            child.setData(0, LOADED_ROLE, False)
            child.setToolTip(1, format_bytes(sub.size))
            child.addChild(self._notice("Loading…"))
            parent.addChild(child)
        for file in folder.files:
            child = self._item(stage_ops.basename(file.name), format_bytes(file.size), FILE)
            child.setData(0, STAGE_ROLE, stage)
            child.setData(0, PATH_ROLE, file.name)
            child.setData(0, DATA_ROLE, file)
            child.setData(0, LOADED_ROLE, True)
            child.setToolTip(0, _file_tooltip(file))
            parent.addChild(child)
        if not folder.folders and not folder.files:
            parent.addChild(self._notice("empty"))
        if truncated:
            parent.addChild(
                self._notice(
                    "Listing stopped at the row cap. Refresh a folder to list it on its own."
                )
            )

    # -- lookups -----------------------------------------------------------

    def stage_item(self, stage: StageRef) -> QTreeWidgetItem | None:
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            kind = item.data(0, KIND_ROLE)
            if kind == STAGE and item.data(0, STAGE_ROLE) == stage:
                return item
            if kind in (DATABASE, SCHEMA):
                stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def _item_for(self, stage: StageRef, prefix: str) -> QTreeWidgetItem | None:
        """The stage's item, or the folder ``prefix`` within it."""
        item = self.stage_item(stage)
        if item is None or not prefix:
            return item
        for part in prefix.rstrip("/").split("/"):
            match = None
            for i in range(item.childCount()):
                child = item.child(i)
                if child is not None and child.text(0) == f"{part}/":
                    match = child
                    break
            if match is None:
                return None
            item = match
        return item

    def upload_target(self, item: QTreeWidgetItem | None) -> tuple[StageRef, str] | None:
        """The stage and folder a drop onto (or upload from) ``item`` goes into."""
        if item is None:
            return None
        stage = item.data(0, STAGE_ROLE)
        if stage is None or not stage.internal:
            return None
        kind = item.data(0, KIND_ROLE)
        path = item.data(0, PATH_ROLE) or ""
        if kind == FILE:
            return stage, stage_ops.folder_of(path)
        if kind in (STAGE, FOLDER):
            return stage, path
        return None

    def selected_in(self, stage: StageRef) -> list[QTreeWidgetItem]:
        """Selected stage, folder and file items that belong to ``stage``."""
        return [
            item
            for item in self.selectedItems()
            if item.data(0, STAGE_ROLE) == stage
            and item.data(0, KIND_ROLE) in (STAGE, FOLDER, FILE)
        ]

    # -- drag and drop from Finder (ST5) -----------------------------------

    @staticmethod
    def _local_paths(urls: list[QUrl]) -> list[str]:
        return [url.toLocalFile() for url in urls if url.isLocalFile()]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._local_paths(event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        if (
            item is not None
            and self._local_paths(event.mimeData().urls())
            and self.upload_target(item)
        ):
            # Highlight where the files will go, the way Finder does.
            self.setCurrentItem(item)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._local_paths(event.mimeData().urls())
        target = self.upload_target(self.itemAt(event.position().toPoint()))
        if not paths or target is None:
            event.ignore()
            return
        event.acceptProposedAction()
        stage, folder = target
        self.files_dropped.emit(stage, folder, paths)

    def filter_tree(self, term: str) -> None:
        needle = term.strip().lower()
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item is not None:
                apply_filter(item, needle)


def _file_tooltip(file: StageFile) -> str:
    lines = [file.name, f"{file.size:,} bytes"]
    if file.last_modified:
        lines.append(f"Modified {file.last_modified}")
    if file.md5:
        lines.append(f"MD5 {file.md5}")
    return "\n".join(lines)


class StagePanel(QWidget):
    """The Stages page: filter, Upload, refresh, the tree, and the progress strip."""

    insert_requested = Signal(str)
    run_requested = Signal(str)
    status_message = Signal(str)
    log_message = Signal(str)

    def __init__(self, controller: StageController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._connected = False
        self._loaded = False
        #: A SHOW STAGES has been sent and not yet answered.
        self._listing = False
        #: What the running transfer is, for its log lines and summary.
        self._plan: TransferPlan | None = None

        self.tree = StageTree(controller, self)
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("filter…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.tree.filter_tree)

        self.upload_button = QPushButton("Upload…", self)
        self.upload_button.clicked.connect(self.upload_to_selection)
        refresh = QPushButton(self)
        refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh.setFixedWidth(32)
        refresh.setToolTip("Refresh the stage list")
        refresh.setAccessibleName("Refresh stages")
        refresh.clicked.connect(self.reload)

        top = QHBoxLayout()
        top.setContentsMargins(8, 8, 8, 0)
        top.setSpacing(4)
        top.addWidget(self.filter, 1)
        top.addWidget(self.upload_button)
        top.addWidget(refresh)

        self.progress_label = QLabel("", self)
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        # A long file name must not push the sidebar wider.
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setToolTip("Stop after the file being transferred now")
        self.stop_button.clicked.connect(self.stop_transfer)
        self.progress_strip = QWidget(self)
        strip = QVBoxLayout(self.progress_strip)
        strip.setContentsMargins(8, 4, 8, 8)
        strip.setSpacing(4)
        strip.addWidget(self.progress_label)
        row = QHBoxLayout()
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.stop_button)
        strip.addLayout(row)
        self.progress_strip.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.progress_strip)

        self.download_action = QAction("Download…", self.tree)
        self.download_action.setShortcut(QKeySequence("Ctrl+D"))
        self.download_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.download_action.triggered.connect(lambda _checked=False: self.download_selection())
        self.tree.addAction(self.download_action)

        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.files_dropped.connect(self.controller.upload)

        controller.stages_ready.connect(self._on_stages)
        controller.stages_failed.connect(self._on_stages_failed)
        controller.plan_ready.connect(self._on_plan_ready)
        controller.transfer_started.connect(self._on_transfer_started)
        controller.progress.connect(self._on_progress)
        controller.file_done.connect(self._on_file_done)
        controller.finished.connect(self._on_finished)
        controller.failed.connect(self._on_failed)
        controller.rejected.connect(self.status_message)

        self.set_connected(False)

    # -- connection --------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        """Forget the listing; it is re-read the next time the tab is shown."""
        self._connected = connected
        self._loaded = False
        self._listing = False
        self.tree.show_message("Loading…" if connected else "Not connected")
        self._update_buttons()

    def ensure_loaded(self) -> None:
        if self._connected and not self._loaded:
            self.reload()

    def reload(self) -> None:
        if not self._connected:
            return
        self._loaded = True
        self._listing = True
        self.tree.show_message("Loading…")
        self.controller.load_stages()

    def open_table_stage(self, database: str, schema: str, table: str) -> None:
        """Show ``@%table`` for a table picked in the Objects tree (ST12)."""
        stage = StageRef(kind=StageKind.TABLE, name=table, database=database, schema=schema)
        self.ensure_loaded()
        # A stage list on its way would replace the item straight away.
        self.tree.add_table_stage(stage, when_listed=self._listing)

    def _on_stages(self, found: list[StageRef]) -> None:
        self._listing = False
        self.tree.populate(found)

    def _on_stages_failed(self, message: str) -> None:
        self._listing = False
        self.tree.show_message(message, error=True)

    def _update_buttons(self) -> None:
        item = self.tree.currentItem()
        target = self.tree.upload_target(item)
        stage = item.data(0, STAGE_ROLE) if item is not None else None
        if stage is not None and not stage.internal:
            tip = "Files can only be uploaded to an internal stage."
        elif self.controller.is_busy:
            tip = "Wait for the running transfer to finish."
        else:
            tip = "Upload files into the selected stage or folder"
        self.upload_button.setToolTip(tip)
        self.upload_button.setEnabled(
            self._connected and target is not None and not self.controller.is_busy
        )

    # -- intents -----------------------------------------------------------

    def upload_to_selection(self) -> None:
        target = self.tree.upload_target(self.tree.currentItem())
        if target is None:
            self.status_message.emit("Select an internal stage or a folder in one to upload to.")
            return
        paths = self.ask_upload_files()
        if paths:
            self.controller.upload(target[0], target[1], paths)

    def download_selection(self, item: QTreeWidgetItem | None = None) -> None:
        item = item or self.tree.currentItem()
        if item is None:
            return
        stage = item.data(0, STAGE_ROLE)
        if stage is None:
            return
        if not stage.internal:
            self.status_message.emit("Files can only be downloaded from an internal stage.")
            return
        selection = [i.data(0, PATH_ROLE) or "" for i in self.tree.selected_in(stage)]
        if not selection:
            selection = [item.data(0, PATH_ROLE) or ""]
        root = self.ask_download_folder()
        if root:
            self.controller.download(stage, selection, root)

    def delete_selection(self, item: QTreeWidgetItem | None = None) -> None:
        item = item or self.tree.currentItem()
        if item is None:
            return
        stage = item.data(0, STAGE_ROLE)
        if stage is None or not stage.internal:
            return
        chosen = [
            i for i in self.tree.selected_in(stage) if i.data(0, KIND_ROLE) in (FOLDER, FILE)
        ] or [item]
        targets: list[StageFile] = []
        labels: list[str] = []
        for i in chosen:
            kind = i.data(0, KIND_ROLE)
            path = i.data(0, PATH_ROLE) or ""
            if kind == FILE:
                targets.append(i.data(0, DATA_ROLE))
                labels.append(path)
            elif kind == FOLDER:
                targets.append(StageFile(name=path, raw=""))
                labels.append(f"{path}  (everything in it, {i.text(1)} listed)")
        if targets and self.confirm_remove(stage, labels):
            self.controller.remove(stage, targets)

    def stop_transfer(self) -> None:
        self.controller.stop()
        self.stop_button.setEnabled(False)
        self.progress_label.setText("Stopping after the current file…")

    # -- dialogs, kept apart so tests can answer them ------------------------

    def ask_upload_files(self) -> list[str]:
        chosen, _filter = QFileDialog.getOpenFileNames(self, "Upload to stage")
        return list(chosen)

    def ask_download_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "Download to folder", str(Path.home()))

    def ask_replace(self, plan: TransferPlan) -> bool | None:
        """Replace (True), Skip (False) or Cancel (None) the files in the way (ST9)."""
        where = (
            "already on the stage"
            if plan.kind is TransferKind.UPLOAD
            else f"already in {plan.local_root}"
        )
        count = len(plan.conflicts)
        shown = "\n".join(plan.conflicts[:CONFLICTS_SHOWN])
        if count > CONFLICTS_SHOWN:
            shown += f"\n…and {count - CONFLICTS_SHOWN:,} more"
        box = message_box(self)
        box.setWindowTitle("Replace files?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"{_plural(count, 'file')} {'is' if count == 1 else 'are'} {where}.")
        box.setInformativeText(shown)
        replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        skip = box.addButton("Skip", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(skip)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is replace:
            return True
        if clicked is skip:
            return False
        return None

    def confirm_remove(self, stage: StageRef, labels: list[str]) -> bool:
        """Confirm a delete, defaulting to keeping the files (ST13)."""
        shown = "\n".join(labels[:CONFLICTS_SHOWN])
        if len(labels) > CONFLICTS_SHOWN:
            shown += f"\n…and {len(labels) - CONFLICTS_SHOWN:,} more"
        box = message_box(self)
        box.setWindowTitle("Delete from stage")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"Delete from {stage_ops.stage_name(stage)}?")
        box.setInformativeText(f"{shown}\n\nYou can't undo this action.")
        delete = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is delete

    # -- transfers ---------------------------------------------------------

    def _on_plan_ready(self, plan: TransferPlan) -> None:
        if not plan.uploads and not plan.downloads and not plan.refused:
            self.controller.abandon(plan)
            noun = "upload" if plan.kind is TransferKind.UPLOAD else "download"
            self.status_message.emit(f"Nothing to {noun}.")
            return
        replace = False
        if plan.conflicts:
            answer = self.ask_replace(plan)
            if answer is None:
                self.controller.abandon(plan)
                self.status_message.emit("Transfer cancelled.")
                return
            replace = answer
        self.controller.confirm(plan, replace)

    def _on_transfer_started(self, plan: TransferPlan) -> None:
        self._plan = plan
        where = stage_ops.stage_name(plan.stage)
        if plan.kind is TransferKind.UPLOAD:
            count = len(plan.uploads) + len(plan.refused)
            self.log_message.emit(f"Uploading {_plural(count, 'file')} to {where}…")
        elif plan.kind is TransferKind.DOWNLOAD:
            count = len(plan.downloads) + len(plan.refused)
            self.log_message.emit(
                f"Downloading {_plural(count, 'file')} from {where} to {plan.local_root}…"
            )
            if any(i.file.name.endswith(".gz") for i in plan.downloads):
                self.log_message.emit(
                    "  Files come down as they are stored: a .gz file stays compressed."
                )
        else:
            self.log_message.emit(f"Deleting from {where}…")
        self.progress_label.setText(f"{_VERBS[plan.kind]}…")
        self.progress_bar.setRange(0, 0)
        self.stop_button.setEnabled(True)
        self.progress_strip.setVisible(True)
        self._update_buttons()

    def _on_progress(self, progress: TransferProgress) -> None:
        if not self.stop_button.isEnabled():
            return  # keep saying "Stopping…"
        text = f"{_VERBS[progress.kind]} {progress.files_done + 1:,} of {progress.files_total:,}"
        if progress.current:
            text += f" · {progress.current}"
        self.progress_label.setText(text)
        if progress.bytes_total:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(1000 * progress.bytes_done / progress.bytes_total))
        elif progress.files_total:
            self.progress_bar.setRange(0, progress.files_total)
            self.progress_bar.setValue(progress.files_done)

    def _on_file_done(self, result: FileResult) -> None:
        detail = f"  ({result.detail})" if result.detail else ""
        self.log_message.emit(f"  {result.status.value:<10} {result.name}{detail}")

    def _on_finished(self, summary: TransferSummary) -> None:
        self._plan = None
        self.progress_strip.setVisible(False)
        self._update_buttons()
        counts = ", ".join(
            f"{count:,} {status.value}"
            for status in FileStatus
            if (count := summary.counts.get(status, 0))
        )
        where = stage_ops.stage_name(summary.stage)
        verb = {
            TransferKind.UPLOAD: f"Upload to {where}",
            TransferKind.DOWNLOAD: f"Download from {where}",
            TransferKind.REMOVE: f"Delete from {where}",
        }[summary.kind]
        if summary.error:
            line = f"{verb} interrupted: {summary.error}"
        elif summary.stopped:
            line = f"{verb} stopped"
        else:
            line = f"{verb} finished"
        line += f": {counts}." if counts else "."
        self.log_message.emit(line)
        self.status_message.emit(line)
        if summary.kind is not TransferKind.DOWNLOAD:
            self.tree.refresh_stage(summary.stage)

    def _on_failed(self, message: str) -> None:
        self.log_message.emit(f"Transfer not started: {message}")
        self.status_message.emit("Transfer not started — see Messages")

    # -- context menu ------------------------------------------------------

    def _show_context_menu(self, position: QPoint) -> None:
        menu = self.build_context_menu(self.tree.itemAt(position))
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def build_context_menu(self, item: QTreeWidgetItem | None) -> QMenu | None:
        if item is None:
            return None
        stage: StageRef | None = item.data(0, STAGE_ROLE)
        kind = item.data(0, KIND_ROLE)
        if stage is None or kind not in (STAGE, FOLDER, FILE):
            return None
        if item not in self.tree.selectedItems():
            self.tree.setCurrentItem(item)
        path = item.data(0, PATH_ROLE) or ""
        busy = self.controller.is_busy

        menu = QMenu(self)
        if stage.internal:
            if kind != FILE:
                upload = menu.addAction("Upload Files…", self.upload_to_selection)
                upload.setEnabled(not busy)
            download = menu.addAction("Download…", lambda: self.download_selection(item))
            download.setShortcut(self.download_action.shortcut())
            download.setEnabled(not busy)
            if kind in (FOLDER, FILE):
                delete = menu.addAction("Delete…", lambda: self.delete_selection(item))
                delete.setEnabled(not busy)
            menu.addSeparator()
        menu.addAction("Copy Stage Path", lambda: self._copy_path(stage, path))
        menu.addAction("Insert Stage Path", lambda: self._insert_path(stage, path))
        menu.addAction(
            "Generate COPY INTO",
            lambda: self._emit_sql(
                self.insert_requested.emit, stage_ops.copy_into_sql, stage, path
            ),
        )
        if kind == FILE:
            file = item.data(0, DATA_ROLE)
            menu.addAction(
                "Generate SELECT",
                lambda: self._emit_sql(
                    self.insert_requested.emit, stage_ops.select_file_sql, stage, file
                ),
            )
        if kind == STAGE and stage.kind is StageKind.NAMED:
            menu.addAction(
                "Describe Stage",
                lambda: self._emit_sql(self.run_requested.emit, stage_ops.describe_sql, stage),
            )
        if kind in (STAGE, FOLDER):
            menu.addSeparator()
            menu.addAction("Refresh", lambda: self.tree.refresh(item))
        return menu

    def _emit_sql(self, sink: Callable[[str], None], build: Callable[..., str], *args: Any) -> None:
        """Build a statement and hand it on, or say why its names were refused."""
        try:
            sql = build(*args)
        except stage_ops.UnsafeName as exc:
            self.status_message.emit(str(exc))
            return
        sink(sql)

    def _copy_path(self, stage: StageRef, path: str) -> None:
        try:
            text = stage_ops.location(stage, path)
        except stage_ops.UnsafeName as exc:
            self.status_message.emit(str(exc))
            return
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(f"Copied {text}")

    def _insert_path(self, stage: StageRef, path: str) -> None:
        self._emit_sql(self.insert_requested.emit, stage_ops.location, stage, path)
