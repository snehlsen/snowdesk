from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt

from snowdesk.model import ColumnInfo
from snowdesk.ui.result_view import ResultModel, ResultView

COLUMNS = [ColumnInfo("ID", "FIXED", 38, 0), ColumnInfo("PAYLOAD", "VARIANT")]


def model(rows=None, exhausted=False, cap=100_000, total=None) -> ResultModel:
    return ResultModel(COLUMNS, rows or [(1, '{"sku":"A1"}')], exhausted, row_cap=cap, total=total)


def test_shape_and_headers(qtbot) -> None:
    m = model()
    assert m.rowCount() == 1
    assert m.columnCount() == 2
    assert m.headerData(0, Qt.Orientation.Horizontal) == "ID"
    assert m.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole) == "NUMBER(38,0)"
    assert m.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole) == "VARIANT"


def test_variant_renders_as_compact_json(qtbot) -> None:
    m = model(rows=[(1, '{\n "sku": "A1"\n}')])
    assert m.data(m.index(0, 1)) == '{"sku":"A1"}'


def test_null_is_styled(qtbot) -> None:
    m = model(rows=[(None, "")])
    index = m.index(0, 0)
    assert m.data(index) == "NULL"
    assert m.data(index, Qt.ItemDataRole.ForegroundRole) is not None
    assert m.data(index, Qt.ItemDataRole.FontRole).italic()
    empty = m.index(0, 1)
    assert m.data(empty) == ""
    assert m.data(empty, Qt.ItemDataRole.FontRole) is None


def test_fetch_more_asks_the_controller_once(qtbot) -> None:
    m = model()
    with qtbot.waitSignal(m.more_requested, timeout=500):
        m.fetchMore(QModelIndex())
    assert not m.canFetchMore(QModelIndex())  # a fetch is in flight
    m.append_rows([(2, "{}")], exhausted=False)
    assert m.canFetchMore(QModelIndex())


def test_no_fetch_when_exhausted(qtbot) -> None:
    assert not model(exhausted=True).canFetchMore(QModelIndex())


def test_rows_are_appended_with_insert_notifications(qtbot) -> None:
    m = model()
    with qtbot.waitSignal(m.rowsInserted, timeout=500):
        m.append_rows([(2, "{}"), (3, "{}")], exhausted=True)
    assert m.rowCount() == 3
    assert m.exhausted


def test_row_cap_truncates_and_notifies(qtbot) -> None:
    m = model(rows=[(0, "{}")], cap=3)
    with qtbot.waitSignal(m.cap_reached, timeout=500):
        m.append_rows([(1, "{}"), (2, "{}"), (3, "{}")], exhausted=False)
    assert m.rowCount() == 3
    assert m.capped
    assert not m.canFetchMore(QModelIndex())
    assert "capped at 3" in m.status_text()


def test_status_text_tracks_progress(qtbot) -> None:
    m = model(rows=[(1, "{}")], total=10)
    assert m.status_text() == "1 of 10 rows"
    m.append_rows([(2, "{}")], exhausted=True)
    assert m.status_text() == "2 rows"


def test_view_copies_selection_as_tsv(qtbot) -> None:
    m = model(rows=[(1, '{"a":1}'), (2, None)])
    view = ResultView("r1", m)
    qtbot.addWidget(view)
    assert view.selected_tsv(with_headers=True) == 'ID\tPAYLOAD\n1\t{"a":1}\n2\tNULL'

    view.table.selectColumn(0)
    assert view.selected_tsv() == "1\n2"


def test_view_shows_a_footer_when_capped(qtbot) -> None:
    m = model(rows=[(1, "{}")], cap=2)
    view = ResultView("r1", m)
    qtbot.addWidget(view)
    assert not view.footer.isVisible()
    m.append_rows([(2, "{}"), (3, "{}")], exhausted=False)
    assert "Row cap reached" in view.footer.text()


def test_view_forwards_fetch_requests_with_its_id(qtbot) -> None:
    m = model()
    view = ResultView("r42", m)
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.more_requested, timeout=500) as blocker:
        m.fetchMore(QModelIndex())
    assert blocker.args == ["r42"]


def test_query_actions_are_off_until_the_view_has_a_query_id(qtbot) -> None:
    view = ResultView("r1", model())
    qtbot.addWidget(view)
    assert not view._copy_qid_action.isEnabled()
    assert not view._profile_action.isEnabled()
    assert view.copy_query_id() is False
    assert view.request_profile() is False

    view.set_query_id("01b0-0001")
    assert view._copy_qid_action.isEnabled()
    assert view._profile_action.isEnabled()


def test_view_asks_for_the_profile_of_its_own_query(qtbot) -> None:
    view = ResultView("r1", model(), query_id="01b0-0007")
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.profile_requested, timeout=500) as blocker:
        view.request_profile()
    assert blocker.args == ["01b0-0007"]
