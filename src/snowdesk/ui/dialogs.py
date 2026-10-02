"""Message boxes that quote rather than interpret, and do not crash.

Qt's message boxes, like its labels, guess at the format of the text they are
given and render anything that looks like markup as markup.  Most of what
SnowDesk puts in a warning came from Snowflake or from the filesystem, which
puts the wording of the box within reach of whoever can provoke the error, so
it goes in as plain text.

Every box is built by :func:`message_box`, which keeps it off the native
``NSAlert``.  On macOS 27 with Qt 6.11 the native alert intermittently brings
the whole app down: AppKit raises while rasterising the alert's symbol icon
during layout, and the uncaught exception aborts the process.  It has hit the
unsaved-changes prompt, the connection warning and the open-transaction
prompt alike, so no box is safe to leave native.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QWidget


def message_box(
    parent: QWidget | None, icon: QMessageBox.Icon = QMessageBox.Icon.Warning
) -> QMessageBox:
    """A message box drawn by Qt rather than by AppKit (see the module note)."""
    box = QMessageBox(parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog)
    box.setIcon(icon)
    return box


def warning_box(parent: QWidget | None, title: str, text: str) -> QMessageBox:
    """Build the warning box.  Separate from showing it, so the format it was
    given can be checked without a modal dialog standing in the way."""
    box = message_box(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    return box


def warn(parent: QWidget | None, title: str, text: str) -> None:
    """Show ``text`` literally in a warning box."""
    warning_box(parent, title, text).exec()


class DialogPrompter:
    """Asks the Session lifecycle's questions with dialogs (its production prompter)."""

    def __init__(self, parent: QWidget, key_file: Callable[[str], str | None]) -> None:
        self.parent = parent
        #: The private key file a Connection uses, to show in the passphrase prompt.
        self.key_file = key_file

    def ask_passphrase(self, connection: str, rejected: bool) -> str | None:
        """Ask for the private key passphrase (C3).

        SnowDesk never writes it anywhere (spec 5, Security).
        """
        headline = (
            "Incorrect passphrase. Try again."
            if rejected
            else f"The private key for \u201c{connection}\u201d is encrypted."
        )
        lines = [headline]
        key_file = self.key_file(connection)
        if key_file:
            lines.append(f"Key: {key_file}")
        lines.extend(["", "Enter the passphrase to unlock it:"])
        passphrase, accepted = QInputDialog.getText(
            self.parent,
            "Private key passphrase",
            "\n".join(lines),
            QLineEdit.EchoMode.Password,
        )
        return passphrase if accepted and passphrase else None

    def ask_settle(self, reason: str) -> bool | None:
        """Commit (True), Roll Back (False), or Cancel (None)."""
        box = message_box(self.parent)
        box.setWindowTitle("Open transaction")
        box.setText("This session has a transaction open.")
        box.setInformativeText(f"{reason} Commit its changes or roll them back?")
        commit = box.addButton("Commit", QMessageBox.ButtonRole.AcceptRole)
        rollback = box.addButton("Roll Back", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(commit)
        box.exec()
        clicked = box.clickedButton()
        if clicked is commit:
            return True
        if clicked is rollback:
            return False
        return None
