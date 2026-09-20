"""Switching appearance from the menu (and following the system)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from snowdesk.ui import theme
from snowdesk.ui.editor import SqlEditor


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Keep the real preferences file out of the tests."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    monkeypatch.setenv("HOME", str(tmp_path))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings().clear()
    yield
    QSettings().clear()


# -- the preference ---------------------------------------------------------


def test_the_default_is_to_follow_the_system(qapp) -> None:
    assert theme.load() is theme.Appearance.SYSTEM


@pytest.mark.parametrize("appearance", list(theme.Appearance))
def test_a_choice_is_remembered(appearance: theme.Appearance, qapp) -> None:
    theme.save(appearance)
    assert theme.load() is appearance


def test_an_unreadable_preference_falls_back_to_the_system(qapp) -> None:
    QSettings().setValue(theme.SETTINGS_KEY, "chartreuse")
    assert theme.load() is theme.Appearance.SYSTEM


def test_every_appearance_has_a_menu_label() -> None:
    labels = [a.label for a in theme.Appearance]
    assert labels == ["Follow System", "Light", "Dark"]


# -- what "dark" means right now --------------------------------------------


def test_the_colour_scheme_decides_when_the_platform_reports_one(qapp, monkeypatch) -> None:
    for scheme, expected in (
        (Qt.ColorScheme.Dark, True),
        (Qt.ColorScheme.Light, False),
    ):
        monkeypatch.setattr(type(qapp.styleHints()), "colorScheme", lambda _self, s=scheme: s)
        assert theme.is_dark(qapp) is expected


def test_window_lightness_is_the_fallback(qapp, monkeypatch) -> None:
    """The offscreen platform reports no scheme, so the palette decides."""
    monkeypatch.setattr(
        type(qapp.styleHints()),
        "colorScheme",
        lambda _self: Qt.ColorScheme.Unknown,
    )
    assert theme.is_dark(qapp) is False  # default palette is light


def test_apply_without_an_application_is_harmless(monkeypatch) -> None:
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    theme.apply(theme.Appearance.DARK)  # must not raise
    assert theme.is_dark() is False


# -- re-theming live widgets ------------------------------------------------


def test_an_editor_can_change_appearance_in_place(qtbot) -> None:
    editor = SqlEditor(None, dark=False)
    qtbot.addWidget(editor)
    editor.setPlainText("select 1 -- note")
    assert editor._dark is False

    editor.set_dark(True)
    assert editor._dark is True

    editor.set_dark(False)
    assert editor._dark is False


def test_the_highlighter_repaints_on_a_change(qtbot) -> None:
    """Syntax colours are not part of Qt's palette, so they must be redone."""
    editor = SqlEditor(None, dark=False)
    qtbot.addWidget(editor)
    editor.setPlainText("select 1")
    light = editor.highlighter._string.foreground().color().name()

    editor.set_dark(True)
    dark = editor.highlighter._string.foreground().color().name()
    assert light != dark
