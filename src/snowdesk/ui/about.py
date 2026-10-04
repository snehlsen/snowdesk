"""The About window.

Laid out the way macOS lays out its own About panels -- icon, name, version,
copyright, centred -- rather than as an alert, and modeless like them: it
does not block the main window, and choosing About again brings the open one
forward.  Being a plain ``QDialog`` it is drawn by Qt either way, so the
native-alert crash in :mod:`snowdesk.ui.dialogs` does not reach it.

Everything a bug report needs is one click away: the version details go on
the clipboard and the log folder opens in Finder.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import Qt, QTimer, QUrl, qVersion
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from snowdesk import __version__, config

PROJECT_URL = "https://github.com/snehlsen/snowdesk"
COPYRIGHT = "© 2026 Sebastian Nehls · MIT License"
ICON_SIZE = 96
#: How long "Copied" stands in for the button's own label.
COPIED_MS = 1500


def version_info() -> str:
    """The versions worth pasting into a bug report, one per line."""
    import snowflake.connector

    mac = platform.mac_ver()[0] or platform.platform()
    return "\n".join(
        [
            f"SnowDesk {__version__}",
            f"macOS {mac} ({platform.machine()})",
            f"Python {sys.version.split()[0]}",
            f"PySide6 {pyside_version}, Qt {qVersion()}",
            f"snowflake-connector-python {snowflake.connector.__version__}",
        ]
    )


def _home_relative(path: Path) -> str:
    """``~/.snowflake`` rather than the full path, which names the account."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _label(text: str, *, secondary: bool = False, size_delta: int = 0) -> QLabel:
    label = QLabel(text)
    # Plain text: the connections path can come from SNOWFLAKE_HOME, and
    # nothing here needs markup except the link, which sets its own format.
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    label.setWordWrap(True)
    if secondary:
        # A role rather than a colour, so it follows a change of appearance.
        label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    if size_delta:
        font = label.font()
        font.setPointSizeF(font.pointSizeF() + size_delta)
        label.setFont(font)
    return label


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About SnowDesk")

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        path = config.icon_path()
        if path is not None:
            self.icon_label.setPixmap(QIcon(str(path)).pixmap(ICON_SIZE, ICON_SIZE))

        name = _label("SnowDesk", size_delta=4)
        bold = name.font()
        bold.setWeight(QFont.Weight.Bold)
        name.setFont(bold)

        self.version_label = _label(f"Version {__version__}", secondary=True)
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.connections_label = _label(
            f"Connections are read from {_home_relative(config.config_dir())}, "
            "the same files the snow CLI uses.",
            secondary=True,
            size_delta=-1,
        )

        self.link_label = QLabel(
            f'<a href="{PROJECT_URL}">{PROJECT_URL.removeprefix("https://")}</a>'
        )
        self.link_label.setTextFormat(Qt.TextFormat.RichText)
        self.link_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.link_label.setOpenExternalLinks(True)

        copyright_label = _label(COPYRIGHT, secondary=True, size_delta=-2)

        self.copy_button = QPushButton("Copy Version Info")
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.copy_version_info)
        self.logs_button = QPushButton("Show Logs in Finder")
        self.logs_button.setAutoDefault(False)
        self.logs_button.clicked.connect(self.show_logs)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.logs_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)
        layout.setContentsMargins(32, 24, 32, 20)
        layout.setSpacing(4)
        layout.addWidget(self.icon_label)
        layout.addSpacing(8)
        layout.addWidget(name)
        layout.addWidget(self.version_label)
        layout.addSpacing(12)
        layout.addWidget(_label("A lightweight macOS client for Snowflake."))
        layout.addWidget(self.connections_label)
        layout.addSpacing(12)
        layout.addWidget(self.link_label)
        layout.addWidget(copyright_label)
        layout.addSpacing(16)
        layout.addLayout(buttons)

        # Esc already closes a dialog; ⌘W is how every other Mac window does.
        QShortcut(QKeySequence(QKeySequence.StandardKey.Close), self, self.close)

        self._restore_copy_label = QTimer(self)
        self._restore_copy_label.setSingleShot(True)
        self._restore_copy_label.setInterval(COPIED_MS)
        self._restore_copy_label.timeout.connect(
            lambda: self.copy_button.setText("Copy Version Info")
        )

    def copy_version_info(self) -> None:
        QGuiApplication.clipboard().setText(version_info())
        self.copy_button.setText("Copied")
        self._restore_copy_label.start()

    def show_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.log_dir())))

    def present(self) -> None:
        """Show the window, or bring it forward if it is already open."""
        self.show()
        self.raise_()
        self.activateWindow()
