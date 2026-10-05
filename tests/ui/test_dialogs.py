"""Message boxes stay off the native NSAlert (see snowdesk.ui.dialogs)."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QMessageBox

import snowdesk
from snowdesk.config import ConnectionInfo
from snowdesk.model import Credentials
from snowdesk.ui.dialogs import PasscodeDialog, PasswordDialog, message_box, warning_box

SRC = Path(snowdesk.__file__).parent

#: Constructing a box, or one of the static helpers that build a native one.
_NATIVE = re.compile(r"QMessageBox\(|QMessageBox\.(about|question|warning|information|critical)\(")


def test_boxes_are_drawn_by_qt(qapp) -> None:
    for box in (message_box(None), warning_box(None, "Title", "Text")):
        assert box.testOption(QMessageBox.Option.DontUseNativeDialog)


def test_every_box_is_built_by_message_box() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        if path.name != "dialogs.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _NATIVE.search(line)
    ]
    assert offenders == [], "build message boxes with snowdesk.ui.dialogs.message_box"


# -- the password prompt -------------------------------------------------------

MFA = ConnectionInfo(name="mfa", user="alice", authenticator="username_password_mfa")
PLAIN = ConnectionInfo(name="plain", user="alice", authenticator="snowflake")


def password_dialog(qtbot, info: ConnectionInfo | None, rejected: str | None = None):
    dialog = PasswordDialog(None, info.name if info else "dev", info, rejected)
    qtbot.addWidget(dialog)
    return dialog


def test_the_password_is_masked_and_required(qtbot) -> None:
    dialog = password_dialog(qtbot, PLAIN)
    assert dialog.password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert not dialog.ok_button.isEnabled()
    dialog.password_edit.setText("hunter2")
    assert dialog.ok_button.isEnabled()


def test_only_an_mfa_connection_asks_for_a_passcode(qtbot) -> None:
    plain = password_dialog(qtbot, PLAIN)
    plain.password_edit.setText("hunter2")
    assert not plain.mfa
    plain.passcode_edit.setText("123456")  # not in the form, so never sent
    assert plain.credentials() == Credentials("hunter2", None)

    mfa = password_dialog(qtbot, MFA)
    assert mfa.passcode_edit.isVisibleTo(mfa)
    mfa.password_edit.setText("hunter2")
    assert mfa.credentials() == Credentials("hunter2", None)
    mfa.passcode_edit.setText("123456")
    assert mfa.credentials() == Credentials("hunter2", "123456")


def test_the_passcode_takes_digits_only(qtbot) -> None:
    dialog = password_dialog(qtbot, MFA)
    qtbot.keyClicks(dialog.passcode_edit, "12ab34")
    assert dialog.passcode_edit.text() == "1234"


def test_snowflakes_reason_is_shown_literally(qtbot) -> None:
    markup = "<b>Incorrect</b> passcode"
    dialog = password_dialog(qtbot, MFA, rejected=markup)
    assert dialog.reason_label.textFormat() is Qt.TextFormat.PlainText
    assert dialog.reason_label.text() == markup


def test_clearing_drops_what_was_typed(qtbot) -> None:
    dialog = password_dialog(qtbot, MFA)
    dialog.password_edit.setText("hunter2")
    dialog.passcode_edit.setText("123456")
    dialog.clear()
    assert dialog.password_edit.text() == ""
    assert dialog.passcode_edit.text() == ""


def test_the_passcode_dialog_asks_for_the_passcode_alone(qtbot) -> None:
    markup = "<b>MFA</b> with TOTP is required"
    dialog = PasscodeDialog(None, "mfa", MFA, markup)
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "password_edit")
    assert dialog.reason_label.textFormat() is Qt.TextFormat.PlainText
    assert dialog.reason_label.text() == markup
    assert not dialog.ok_button.isEnabled()
    qtbot.keyClicks(dialog.passcode_edit, "12ab3456")
    assert dialog.ok_button.isEnabled()
    assert dialog.passcode() == "123456"
    dialog.clear()
    assert dialog.passcode() is None
