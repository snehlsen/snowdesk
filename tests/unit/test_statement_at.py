"""Which statement ⌘↩ runs (Q1)."""

from __future__ import annotations

import pytest

from snowdesk.controllers.query import statement_at

TWO_LINES = "select 1;\nselect 2;"


def at(text: str, position: int) -> str | None:
    found = statement_at(text, position)
    return found.sql if found else None


def after(text: str, marker: str) -> int:
    """Cursor position just past ``marker``."""
    return text.index(marker) + len(marker)


def test_cursor_at_the_end_of_a_line_runs_that_line() -> None:
    """The reported bug: finishing a line and pressing ⌘↩ ran the next one."""
    assert at(TWO_LINES, after(TWO_LINES, "select 1;")) == "select 1"


def test_every_position_on_the_first_line_belongs_to_it() -> None:
    end_of_line = after(TWO_LINES, "select 1;")
    assert {at(TWO_LINES, p) for p in range(end_of_line + 1)} == {"select 1"}


def test_the_start_of_the_next_line_belongs_to_the_next_statement() -> None:
    assert at(TWO_LINES, TWO_LINES.index("select 2")) == "select 2"


def test_inside_a_statement() -> None:
    assert at(TWO_LINES, 3) == "select 1"
    assert at(TWO_LINES, after(TWO_LINES, "select 2")) == "select 2"


def test_trailing_spaces_after_the_semicolon_still_belong_to_it() -> None:
    text = "select 1;   \nselect 2;"
    for position in range(text.index("\n") + 1):
        assert at(text, position) == "select 1"


def test_two_statements_on_one_line_split_at_the_second() -> None:
    text = "select 1; select 2;"
    assert at(text, after(text, "select 1;")) == "select 1"
    assert at(text, text.index("select 2")) == "select 2"


def test_cursor_on_a_blank_line_takes_the_next_statement() -> None:
    text = "select 1;\n\nselect 2;"
    assert at(text, text.index("\n\n") + 1) == "select 2"


def test_end_of_the_last_statement() -> None:
    assert at(TWO_LINES, len(TWO_LINES)) == "select 2"


def test_a_single_statement_is_always_chosen() -> None:
    text = "select 1;"
    assert {at(text, p) for p in range(len(text) + 1)} == {"select 1"}


def test_without_a_terminator_the_buffer_is_one_statement() -> None:
    """Nothing separates these two lines, so they are a single statement."""
    text = "select 1\nselect 2"
    assert at(text, after(text, "select 1")) == "select 1\nselect 2"


def test_multiline_statement_holds_the_cursor_throughout() -> None:
    text = "select *\nfrom orders\nwhere id = 1;\nselect 2;"
    for marker in ("select *", "from orders", "where id = 1;"):
        assert at(text, after(text, marker)) == "select *\nfrom orders\nwhere id = 1"


def test_a_trailing_comment_stays_with_the_statement_it_follows() -> None:
    """The connector's splitter keeps a trailing line comment attached.

    So the first statement's text carries the comment, and its own semicolon
    with it; the cursor anywhere on that line runs that statement.
    """
    text = "select 1; -- note\nselect 2;"
    for position in range(text.index("\n") + 1):
        assert at(text, position) == "select 1; -- note"
    assert at(text, text.index("select 2")) == "select 2"


def test_empty_input_has_no_statement() -> None:
    assert statement_at("", 0) is None
    assert statement_at("   \n\n", 3) is None


@pytest.mark.parametrize("position", [-5, 0, 1000])
def test_positions_outside_the_text_do_not_raise(position: int) -> None:
    assert statement_at(TWO_LINES, position) is not None
