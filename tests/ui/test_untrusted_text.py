"""Text the account controls is shown, not interpreted.

Role, warehouse, database and schema names come back from Snowflake, and error
messages are written by it, so anyone who can name an object or provoke an
error gets a say in what these widgets display.  Qt guesses at the format of
what it is given, which would let that text redecorate the window.
"""

# ruff: noqa: F811 -- a fixture imported by name is shadowed by the parameter
# of every test that asks for it, which is how pytest fixtures are shared.
from __future__ import annotations

from PySide6.QtCore import Qt

from snowdesk.model import SessionContext
from snowdesk.ui.dialogs import warning_box

from .test_main_window import harness  # noqa: F401  (fixture)

MARKUP = '<b>PROD</b><img src="/tmp/pixel.png">'


def test_context_label_shows_markup_literally(harness) -> None:
    window = harness.window
    window._set_context(SessionContext(role=MARKUP, warehouse="WH"))

    assert window.context_label.textFormat() is Qt.TextFormat.PlainText
    assert MARKUP in window.context_label.text()


def test_banner_shows_the_connectors_wording_literally(harness) -> None:
    window = harness.window
    window.show_banner(f"Connection lost — {MARKUP}")

    assert window.banner_label.textFormat() is Qt.TextFormat.PlainText
    assert MARKUP in window.banner_label.text()


def test_the_state_dot_is_still_markup(harness) -> None:
    """The one label that is rich text on purpose: SnowDesk writes it itself."""
    assert harness.window.state_label.textFormat() is Qt.TextFormat.RichText


def test_warning_box_quotes_what_it_is_given(qtbot) -> None:
    box = warning_box(None, "Could not connect", MARKUP)
    qtbot.addWidget(box)
    assert box.textFormat() is Qt.TextFormat.PlainText
    assert box.text() == MARKUP
