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

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from snowdesk.config import ConnectionInfo
from snowdesk.model import Credentials

#: The connector's name for password-plus-MFA sign-in, as written in the file.
MFA_AUTHENTICATOR = "username_password_mfa"


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


def _plain_label(text: str) -> QLabel:
    """A label for text that came from the config file or from Snowflake."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _passcode_edit() -> QLineEdit:
    """A field for a one-time MFA passcode: digits only.

    Shown as typed, the way authenticator apps show it, since it stops
    working within a minute.
    """
    edit = QLineEdit()
    edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,10}"), edit))
    return edit


def _connect_buttons(dialog: QDialog, required: QLineEdit) -> tuple[QDialogButtonBox, QWidget]:
    """Connect and Cancel, with Connect held back until ``required`` has text."""
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok.setText("Connect")
    ok.setEnabled(False)
    required.textChanged.connect(lambda text: ok.setEnabled(bool(text)))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons, ok


class PasswordDialog(QDialog):
    """Asks for a Connection's password, and its MFA passcode if it uses MFA.

    Not a ``QInputDialog``: that holds one field, and MFA needs two.  The
    password field is a password-mode ``QLineEdit``, so it is masked and
    refuses copy and drag.
    """

    def __init__(
        self,
        parent: QWidget | None,
        connection: str,
        info: ConnectionInfo | None,
        rejected: str | None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Password required")
        self.mfa = bool(info and (info.authenticator or "").lower() == MFA_AUTHENTICATOR)

        layout = QVBoxLayout(self)
        if rejected is None:
            layout.addWidget(_plain_label(f"\u201c{connection}\u201d has no password configured."))
        else:
            layout.addWidget(_plain_label("Snowflake did not accept the sign-in:"))
            #: Snowflake's own wording, which says whether the password or the
            #: passcode was the problem.
            self.reason_label = _plain_label(rejected)
            layout.addWidget(self.reason_label)

        form = QFormLayout()
        if info and info.user:
            form.addRow("User:", _plain_label(info.user))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.password_edit)
        self.passcode_edit = _passcode_edit()
        self.passcode_edit.setPlaceholderText("Optional")
        if self.mfa:
            form.addRow("MFA passcode:", self.passcode_edit)
        layout.addLayout(form)
        if self.mfa:
            hint = _plain_label(
                "Leave the passcode empty to use a cached MFA token or to approve "
                "a push on your phone."
            )
            hint.setEnabled(False)
            layout.addWidget(hint)

        buttons, self.ok_button = _connect_buttons(self, self.password_edit)
        layout.addWidget(buttons)
        self.password_edit.setFocus()

    def credentials(self) -> Credentials | None:
        password = self.password_edit.text()
        if not password:
            return None
        passcode = self.passcode_edit.text() if self.mfa else ""
        return Credentials(password, passcode or None)

    def clear(self) -> None:
        """Drop the typed text, so the widgets do not hold on to it."""
        self.password_edit.clear()
        self.passcode_edit.clear()


class PasscodeDialog(QDialog):
    """Asks for just an MFA passcode, for a Connection whose password is configured."""

    def __init__(
        self, parent: QWidget | None, connection: str, info: ConnectionInfo | None, reason: str
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("MFA passcode required")

        layout = QVBoxLayout(self)
        layout.addWidget(
            _plain_label(f"Signing in to \u201c{connection}\u201d needs an MFA passcode:")
        )
        #: Snowflake's own wording: a passcode is required, or the last one
        #: was wrong, or the push was denied.
        self.reason_label = _plain_label(reason)
        layout.addWidget(self.reason_label)

        form = QFormLayout()
        if info and info.user:
            form.addRow("User:", _plain_label(info.user))
        self.passcode_edit = _passcode_edit()
        form.addRow("MFA passcode:", self.passcode_edit)
        layout.addLayout(form)

        buttons, self.ok_button = _connect_buttons(self, self.passcode_edit)
        layout.addWidget(buttons)
        self.passcode_edit.setFocus()

    def passcode(self) -> str | None:
        return self.passcode_edit.text() or None

    def clear(self) -> None:
        self.passcode_edit.clear()


class DialogPrompter:
    """Asks the Session lifecycle's questions with dialogs (its production prompter)."""

    def __init__(
        self, parent: QWidget, connection_info: Callable[[str], ConnectionInfo | None]
    ) -> None:
        self.parent = parent
        #: What the config files say about a Connection: which key file to
        #: name, which user to sign in as, and whether to ask for a passcode.
        self.connection_info = connection_info

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
        info = self.connection_info(connection)
        key_file = info.private_key_file if info else None
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

    def ask_password(self, connection: str, rejected: str | None) -> Credentials | None:
        """Ask for the password, and the MFA passcode if the Connection uses MFA.

        SnowDesk never writes either anywhere (spec 5, Security).
        """
        dialog = PasswordDialog(self.parent, connection, self.connection_info(connection), rejected)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.credentials()
        finally:
            dialog.clear()
            dialog.deleteLater()

    def ask_passcode(self, connection: str, reason: str) -> str | None:
        """Ask for an MFA passcode only; the password comes from the config file."""
        dialog = PasscodeDialog(self.parent, connection, self.connection_info(connection), reason)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.passcode()
        finally:
            dialog.clear()
            dialog.deleteLater()

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
