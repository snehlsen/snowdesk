"""Appearance: follow the system, or force light or dark (S1, spec 7.8).

Qt applies the colour scheme to its own palette, which covers native chrome.
SnowDesk's hand-picked colours -- syntax highlighting, the current-line
marker -- are not part of that palette, so they are re-applied whenever the
scheme changes, including when the *system* appearance changes while the app
is running.
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

SETTINGS_KEY = "appearance"

#: Below this the window colour counts as dark.  Only used as a fallback on
#: platforms that do not report a colour scheme.
_DARK_LIGHTNESS = 128


class Appearance(StrEnum):
    """What the user asked for, which is not the same as what is showing."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return {"system": "Follow System", "light": "Light", "dark": "Dark"}[self.value]


_SCHEMES = {
    Appearance.SYSTEM: Qt.ColorScheme.Unknown,
    Appearance.LIGHT: Qt.ColorScheme.Light,
    Appearance.DARK: Qt.ColorScheme.Dark,
}


def _app(app: QApplication | None) -> QApplication | None:
    """QApplication.instance() is typed as the base class; narrow it."""
    if app is not None:
        return app
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else None


def apply(appearance: Appearance, app: QApplication | None = None) -> None:
    """Ask Qt for this colour scheme. ``SYSTEM`` hands control back to macOS."""
    target = _app(app)
    if target is None:
        return
    target.styleHints().setColorScheme(_SCHEMES[appearance])


def is_dark(app: QApplication | None = None) -> bool:
    """Whether dark colours should be used *right now*.

    The colour scheme is authoritative where the platform reports one; the
    window lightness is the fallback for those that do not, such as the
    offscreen platform used by the tests.
    """
    target = _app(app)
    if target is None:
        return False
    scheme = target.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    window = target.palette().color(QPalette.ColorRole.Window)
    return window.lightness() < _DARK_LIGHTNESS


def load() -> Appearance:
    """The saved preference, defaulting to following the system."""
    raw = QSettings().value(SETTINGS_KEY, Appearance.SYSTEM.value)
    try:
        return Appearance(str(raw))
    except ValueError:
        return Appearance.SYSTEM


def save(appearance: Appearance) -> None:
    QSettings().setValue(SETTINGS_KEY, appearance.value)
