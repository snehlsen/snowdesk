"""Message boxes that quote rather than interpret.

Qt's message boxes, like its labels, guess at the format of the text they are
given and render anything that looks like markup as markup.  Most of what
SnowDesk puts in a warning came from Snowflake or from the filesystem, which
puts the wording of the box within reach of whoever can provoke the error, so
it goes in as plain text.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget


def warning_box(parent: QWidget | None, title: str, text: str) -> QMessageBox:
    """Build the warning box.  Separate from showing it, so the format it was
    given can be checked without a modal dialog standing in the way."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    return box


def warn(parent: QWidget | None, title: str, text: str) -> None:
    """Show ``text`` literally in a warning box."""
    warning_box(parent, title, text).exec()
