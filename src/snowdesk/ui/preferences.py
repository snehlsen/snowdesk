"""Preferences (S1, spec 7.8).

Stored with ``QSettings``.  Page size and row cap are read when a query runs
rather than held by the widgets, so a change takes effect on the next run
without anything having to be rebuilt; font size and appearance apply to the
open window immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from snowdesk.config import DEFAULT_PAGE_SIZE, DEFAULT_ROW_CAP
from snowdesk.ui import theme

DEFAULT_FONT_SIZE = 13
MIN_FONT_SIZE = 9
MAX_FONT_SIZE = 24


@dataclass(frozen=True, slots=True)
class Preferences:
    """Everything the preferences dialog controls."""

    page_size: int = DEFAULT_PAGE_SIZE
    row_cap: int = DEFAULT_ROW_CAP
    font_size: int = DEFAULT_FONT_SIZE
    appearance: theme.Appearance = theme.Appearance.SYSTEM


def _int(settings: QSettings, key: str, default: int, low: int, high: int) -> int:
    """Read an int, falling back to the default if it is missing or nonsense."""
    raw = settings.value(key, default)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def load() -> Preferences:
    settings = QSettings()
    return Preferences(
        page_size=_int(settings, "page_size", DEFAULT_PAGE_SIZE, 50, 100_000),
        row_cap=_int(settings, "row_cap", DEFAULT_ROW_CAP, 1_000, 10_000_000),
        font_size=_int(settings, "font_size", DEFAULT_FONT_SIZE, MIN_FONT_SIZE, MAX_FONT_SIZE),
        appearance=theme.load(),
    )


def save(prefs: Preferences) -> None:
    settings = QSettings()
    settings.setValue("page_size", prefs.page_size)
    settings.setValue("row_cap", prefs.row_cap)
    settings.setValue("font_size", prefs.font_size)
    theme.save(prefs.appearance)


class PreferencesDialog(QDialog):
    """A plain settings sheet; macOS opens it from the application menu."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SnowDesk Settings")
        self.setModal(True)
        current = load()

        self.page_size = QSpinBox(self)
        self.page_size.setRange(50, 100_000)
        self.page_size.setSingleStep(100)
        self.page_size.setValue(current.page_size)
        self.page_size.setToolTip("Rows fetched per request while scrolling")

        self.row_cap = QSpinBox(self)
        self.row_cap.setRange(1_000, 10_000_000)
        self.row_cap.setSingleStep(10_000)
        self.row_cap.setGroupSeparatorShown(True)
        self.row_cap.setValue(current.row_cap)
        self.row_cap.setToolTip("Most rows held in memory for one result")

        self.font_size = QSpinBox(self)
        self.font_size.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_size.setValue(current.font_size)
        self.font_size.setSuffix(" pt")

        self.appearance = QComboBox(self)
        for option in theme.Appearance:
            self.appearance.addItem(option.label, option.value)
        self.appearance.setCurrentIndex(list(theme.Appearance).index(current.appearance))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Rows per fetch:", self.page_size)
        form.addRow("Row cap:", self.row_cap)
        form.addRow("Editor font size:", self.font_size)
        form.addRow("Appearance:", self.appearance)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> Preferences:
        return Preferences(
            page_size=self.page_size.value(),
            row_cap=self.row_cap.value(),
            font_size=self.font_size.value(),
            appearance=theme.Appearance(str(self.appearance.currentData())),
        )
