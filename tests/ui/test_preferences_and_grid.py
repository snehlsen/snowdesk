"""Preferences (S1), client-side sort (R7) and the cell detail pane (R8)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from PySide6.QtCore import QSettings, Qt

from snowdesk.model import ColumnInfo
from snowdesk.ui import preferences, theme
from snowdesk.ui.result_view import ResultModel, ResultView

COLUMNS = [
    ColumnInfo("ID", "FIXED", 38, 0),
    ColumnInfo("NAME", "TEXT"),
    ColumnInfo("PAYLOAD", "VARIANT"),
]


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings().clear()
    yield
    QSettings().clear()


# -- preferences (S1) -------------------------------------------------------


def test_defaults_when_nothing_is_stored(qapp) -> None:
    prefs = preferences.load()
    assert prefs.page_size == 500
    assert prefs.row_cap == 100_000
    assert prefs.appearance is theme.Appearance.SYSTEM


def test_formula_escaping_defaults_on(qapp) -> None:
    """Off by default would mean shipping the safe behaviour switched off."""
    assert preferences.load().escape_formulas is True


def test_formula_escaping_persists_when_turned_off(qapp) -> None:
    preferences.save(preferences.Preferences(escape_formulas=False))
    assert preferences.load().escape_formulas is False


def test_formula_escaping_survives_the_round_trip_through_settings(qapp) -> None:
    """QSettings hands back the string it wrote, not the bool it was given."""
    QSettings().setValue("escape_formulas", "false")
    assert preferences.load().escape_formulas is False
    QSettings().setValue("escape_formulas", "true")
    assert preferences.load().escape_formulas is True


def test_preferences_persist(qapp) -> None:
    preferences.save(
        preferences.Preferences(
            page_size=250, row_cap=5_000, font_size=16, appearance=theme.Appearance.DARK
        )
    )
    loaded = preferences.load()
    assert (loaded.page_size, loaded.row_cap, loaded.font_size) == (250, 5_000, 16)
    assert loaded.appearance is theme.Appearance.DARK


def test_nonsense_values_fall_back_to_the_defaults(qapp) -> None:
    QSettings().setValue("page_size", "not a number")
    assert preferences.load().page_size == 500


def test_values_are_clamped_to_something_workable(qapp) -> None:
    QSettings().setValue("page_size", 1)
    QSettings().setValue("row_cap", 99_999_999_999)
    prefs = preferences.load()
    assert prefs.page_size == 50
    assert prefs.row_cap == 10_000_000


def test_the_dialog_round_trips_its_values(qtbot) -> None:
    preferences.save(
        preferences.Preferences(
            page_size=750, row_cap=20_000, font_size=15, appearance=theme.Appearance.LIGHT
        )
    )
    dialog = preferences.PreferencesDialog()
    qtbot.addWidget(dialog)
    assert dialog.values() == preferences.load()


# -- client-side sort (R7) --------------------------------------------------


def make_view(qtbot, rows, exhausted=True):
    model = ResultModel(COLUMNS, rows, exhausted)
    view = ResultView("r1", model)
    qtbot.addWidget(view)
    return view


def test_rows_keep_the_query_order_until_a_header_is_clicked(qtbot) -> None:
    """Enabling sorting must not silently reorder the result."""
    rows = [(3, "c", "{}"), (1, "a", "{}"), (2, "b", "{}")]
    view = make_view(qtbot, rows)
    assert [r[0] for r in view.model.rows] == [3, 1, 2]
    assert view.model.sorted_column is None


def test_sorting_orders_the_loaded_rows(qtbot) -> None:
    view = make_view(qtbot, [(3, "c", "{}"), (1, "a", "{}"), (2, "b", "{}")])
    view.model.sort(0, Qt.SortOrder.AscendingOrder)
    assert [r[0] for r in view.model.rows] == [1, 2, 3]
    view.model.sort(0, Qt.SortOrder.DescendingOrder)
    assert [r[0] for r in view.model.rows] == [3, 2, 1]


def test_nulls_sort_last_rather_than_exploding(qtbot) -> None:
    view = make_view(qtbot, [(2, "b", "{}"), (None, None, "{}"), (1, "a", "{}")])
    view.model.sort(0, Qt.SortOrder.AscendingOrder)
    assert [r[0] for r in view.model.rows] == [1, 2, None]


def test_mixed_types_in_one_column_do_not_raise(qtbot) -> None:
    """A VARIANT column can hold anything at all."""
    rows = [
        (1, "text", Decimal("2.5")),
        (2, "more", dt.date(2026, 1, 1)),
        (3, "text", None),
    ]
    view = make_view(qtbot, rows)
    view.model.sort(2, Qt.SortOrder.AscendingOrder)
    assert len(view.model.rows) == 3


def test_sorting_a_partial_result_says_so(qtbot) -> None:
    view = make_view(qtbot, [(2, "b", "{}"), (1, "a", "{}")], exhausted=False)
    assert not view.footer.isVisibleTo(view)
    view.model.sort(0, Qt.SortOrder.AscendingOrder)
    assert view.footer.isVisibleTo(view)
    assert "loaded so far" in view.footer.text()


def test_sorting_a_complete_result_needs_no_caveat(qtbot) -> None:
    view = make_view(qtbot, [(2, "b", "{}"), (1, "a", "{}")], exhausted=True)
    view.model.sort(0, Qt.SortOrder.AscendingOrder)
    assert not view.footer.isVisibleTo(view)


def test_an_out_of_range_column_is_ignored(qtbot) -> None:
    view = make_view(qtbot, [(1, "a", "{}")])
    view.model.sort(99, Qt.SortOrder.AscendingOrder)
    assert view.model.sorted_column is None


# -- cell detail (R8) -------------------------------------------------------


def test_the_pane_is_hidden_until_asked_for(qtbot) -> None:
    view = make_view(qtbot, [(1, "a", '{"sku":"A1"}')])
    assert not view.detail_is_visible()
    view.set_detail_visible(True)
    assert view.detail_is_visible()


def test_json_is_pretty_printed(qtbot) -> None:
    view = make_view(qtbot, [(1, "a", '{"sku":"A1","qty":2}')])
    view.set_detail_visible(True)
    view.table.setCurrentIndex(view.model.index(0, 2))
    assert view.current_cell_text() == '{\n  "sku": "A1",\n  "qty": 2\n}'


def test_other_values_are_shown_as_they_are(qtbot) -> None:
    view = make_view(qtbot, [(1, "a long name", "{}")])
    view.set_detail_visible(True)
    view.table.setCurrentIndex(view.model.index(0, 1))
    assert view.current_cell_text() == "a long name"


def test_null_is_named_rather_than_blank(qtbot) -> None:
    view = make_view(qtbot, [(1, None, "{}")])
    view.set_detail_visible(True)
    view.table.setCurrentIndex(view.model.index(0, 1))
    assert view.current_cell_text() == "NULL"


def test_no_selection_shows_nothing(qtbot) -> None:
    view = make_view(qtbot, [(1, "a", "{}")])
    view.set_detail_visible(True)
    view.table.setCurrentIndex(view.model.index(-1, -1))
    assert view.current_cell_text() == ""
