"""The About window (see snowdesk.ui.about)."""

# ruff: noqa: F811 -- a fixture imported by name is shadowed by the parameter
# of every test that asks for it, which is how pytest fixtures are shared.
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QGuiApplication

from snowdesk import __version__, config
from snowdesk.ui.about import AboutDialog, version_info

from .test_main_window import harness  # noqa: F401  (fixture)


@pytest.fixture
def about(qtbot) -> AboutDialog:
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    return dialog


def _open_about(harness) -> AboutDialog:
    next(a for a in harness.window.actions() if a.text() == "About SnowDesk").trigger()
    return harness.window._about


def test_shows_the_app_icon(about: AboutDialog) -> None:
    # The installed app once showed its About box without the logo.
    assert not about.icon_label.pixmap().isNull()


def test_shows_the_version(about: AboutDialog) -> None:
    assert about.version_label.text() == f"Version {__version__}"


def test_version_info_names_every_version_a_bug_report_needs(about: AboutDialog) -> None:
    info = version_info()
    for name in ("SnowDesk", "macOS", "Python", "PySide6", "Qt", "snowflake-connector-python"):
        assert name in info
    assert __version__ in info


def test_copy_version_info_puts_it_on_the_clipboard(about: AboutDialog) -> None:
    about.copy_button.click()
    assert QGuiApplication.clipboard().text() == version_info()
    assert about.copy_button.text() == "Copied"


def test_show_logs_opens_the_log_folder(
    about: AboutDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    opened: list[QUrl] = []
    monkeypatch.setattr("snowdesk.ui.about.QDesktopServices.openUrl", opened.append)
    about.logs_button.click()
    assert opened == [QUrl.fromLocalFile(str(tmp_path))]


def test_connections_folder_is_shown_relative_to_home(qtbot, monkeypatch) -> None:
    monkeypatch.delenv("SNOWFLAKE_HOME", raising=False)
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    assert "~/.snowflake" in dialog.connections_label.text()
    assert str(Path.home()) not in dialog.connections_label.text()


def test_connections_folder_is_shown_literally(qtbot, monkeypatch) -> None:
    # SNOWFLAKE_HOME is set outside the app; it is a path, not markup.
    markup = "/tmp/<b>x</b>"
    monkeypatch.setenv("SNOWFLAKE_HOME", markup)
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    assert dialog.connections_label.textFormat() is Qt.TextFormat.PlainText
    assert markup in dialog.connections_label.text()


def test_about_does_not_block_the_main_window(harness) -> None:
    about = _open_about(harness)
    assert about.isVisible()
    assert not about.isModal()


def test_choosing_about_again_reuses_the_open_window(harness) -> None:
    first = _open_about(harness)
    assert _open_about(harness) is first
