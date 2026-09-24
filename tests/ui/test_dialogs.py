"""Message boxes stay off the native NSAlert (see snowdesk.ui.dialogs)."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

import snowdesk
from snowdesk.ui.dialogs import message_box, warning_box

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
