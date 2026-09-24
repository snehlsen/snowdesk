"""The object browser tree and its context menu (B1-B5)."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QGuiApplication

from snowdesk.controllers.browser import BrowserController
from snowdesk.db import browser as browse
from snowdesk.db.worker import SnowflakeWorker
from snowdesk.model import ObjectNode
from snowdesk.ui.object_tree import KIND_ROLE, ObjectTree

DATABASES = [ObjectNode(name="RAW", kind=browse.DATABASE, detail="Database", path=("RAW",))]
SCHEMAS = [ObjectNode(name="PUBLIC", kind=browse.SCHEMA, detail="Schema", path=("RAW", "PUBLIC"))]
OBJECTS = [
    ObjectNode(name="ORDERS", kind=browse.TABLE, detail="Table", path=("RAW", "PUBLIC", "ORDERS")),
    ObjectNode(
        name="V_Orders", kind=browse.VIEW, detail="View", path=("RAW", "PUBLIC", "V_Orders")
    ),
]
COLUMNS = [
    ObjectNode(
        name="ORDER_ID",
        kind=browse.COLUMN,
        detail="NUMBER(38,0)",
        path=("RAW", "PUBLIC", "ORDERS", "ORDER_ID"),
    ),
    ObjectNode(
        name="order date",
        kind=browse.COLUMN,
        detail="DATE",
        path=("RAW", "PUBLIC", "ORDERS", "order date"),
    ),
]


@pytest.fixture
def tree(qtbot):
    controller = BrowserController(SnowflakeWorker())
    widget = ObjectTree(controller)
    qtbot.addWidget(widget)
    # Populate through the controller so its per-node cache fills too.
    controller._on_nodes((), DATABASES)
    controller._on_nodes(("RAW",), SCHEMAS)
    controller._on_nodes(("RAW", "PUBLIC"), OBJECTS)
    return widget


def item_for(tree: ObjectTree, path: tuple[str, ...]):
    found = tree._item_for_path(path)
    assert found is not None, f"no item at {path}"
    return found


def menu_for(tree: ObjectTree, path: tuple[str, ...]) -> list[str]:
    """Build the context menu for a node and return its entries."""
    menu = tree.build_context_menu(item_for(tree, path))
    assert menu is not None, f"no menu for {path}"
    return [a.text() for a in menu.actions() if a.text()]


# -- tree contents (B1, B2) -------------------------------------------------


def test_levels_are_populated_lazily(tree: ObjectTree) -> None:
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == ["RAW"]
    schema = item_for(tree, ("RAW", "PUBLIC"))
    assert [schema.child(i).text(0) for i in range(schema.childCount())] == ["ORDERS", "V_Orders"]


def test_columns_show_their_types(tree: ObjectTree) -> None:
    tree.controller._on_nodes(("RAW", "PUBLIC", "ORDERS"), COLUMNS)
    table = item_for(tree, ("RAW", "PUBLIC", "ORDERS"))
    assert [
        (table.child(i).text(0), table.child(i).text(1)) for i in range(table.childCount())
    ] == [
        ("ORDER_ID", "NUMBER(38,0)"),
        ("order date", "DATE"),
    ]


def test_views_and_tables_are_distinguished(tree: ObjectTree) -> None:
    assert item_for(tree, ("RAW", "PUBLIC", "ORDERS")).data(0, KIND_ROLE) == browse.TABLE
    assert item_for(tree, ("RAW", "PUBLIC", "V_Orders")).data(0, KIND_ROLE) == browse.VIEW


# -- context menu (B4) ------------------------------------------------------


def test_table_menu_offers_every_action(tree: ObjectTree) -> None:
    assert menu_for(tree, ("RAW", "PUBLIC", "ORDERS")) == [
        "Preview 100 Rows",
        "Generate SELECT",
        "Show Table Stage",
        "Insert Name",
        "Copy Name",
        "Show DDL",
        "Refresh",
    ]


def test_database_menu_omits_table_only_actions(tree: ObjectTree) -> None:
    entries = menu_for(tree, ("RAW",))
    assert "Preview 100 Rows" not in entries
    assert "Generate SELECT" not in entries
    assert {"Insert Name", "Copy Name", "Show DDL", "Refresh"} <= set(entries)


def test_column_menu_has_no_ddl_or_refresh(tree: ObjectTree) -> None:
    tree.controller._on_nodes(("RAW", "PUBLIC", "ORDERS"), COLUMNS)
    item_for(tree, ("RAW", "PUBLIC", "ORDERS")).setExpanded(True)
    entries = menu_for(tree, ("RAW", "PUBLIC", "ORDERS", "ORDER_ID"))
    assert entries == ["Insert Name", "Copy Name"]


def test_preview_asks_for_a_hundred_rows(tree: ObjectTree, qtbot) -> None:
    with qtbot.waitSignal(tree.run_requested, timeout=500) as blocker:
        tree._preview(("RAW", "PUBLIC", "ORDERS"))
    assert blocker.args == ["SELECT * FROM RAW.PUBLIC.ORDERS LIMIT 100"]


def test_generate_select_uses_loaded_columns(tree: ObjectTree, qtbot) -> None:
    tree.controller._on_nodes(("RAW", "PUBLIC", "ORDERS"), COLUMNS)
    with qtbot.waitSignal(tree.insert_requested, timeout=500) as blocker:
        tree._generate_select(("RAW", "PUBLIC", "ORDERS"))
    assert blocker.args == ['SELECT ORDER_ID,\n       "order date"\nFROM RAW.PUBLIC.ORDERS']


def test_generate_select_falls_back_before_columns_are_loaded(tree: ObjectTree, qtbot) -> None:
    with qtbot.waitSignal(tree.insert_requested, timeout=500) as blocker:
        tree._generate_select(("RAW", "PUBLIC", "V_Orders"))
    assert blocker.args == ['SELECT *\nFROM RAW.PUBLIC."V_Orders"']


def test_show_ddl_uses_the_node_kind(tree: ObjectTree, qtbot) -> None:
    with qtbot.waitSignal(tree.run_requested, timeout=500) as blocker:
        tree._show_ddl(("RAW", "PUBLIC", "V_Orders"), browse.VIEW)
    assert blocker.args == ["""SELECT GET_DDL('VIEW', 'RAW.PUBLIC."V_Orders"')"""]


def test_copy_name_puts_a_qualified_name_on_the_clipboard(tree: ObjectTree) -> None:
    tree._copy_name(("RAW", "PUBLIC", "V_Orders"), browse.VIEW)
    assert QGuiApplication.clipboard().text() == 'RAW.PUBLIC."V_Orders"'


def test_copy_name_for_a_column_is_just_the_column(tree: ObjectTree) -> None:
    tree._copy_name(("RAW", "PUBLIC", "ORDERS", "order date"), browse.COLUMN)
    assert QGuiApplication.clipboard().text() == '"order date"'


# -- insert and refresh (B3, B5) --------------------------------------------


def test_double_click_inserts_the_qualified_name(tree: ObjectTree, qtbot) -> None:
    item = item_for(tree, ("RAW", "PUBLIC", "V_Orders"))
    with qtbot.waitSignal(tree.insert_requested, timeout=500) as blocker:
        tree._on_double_clicked(item, 0)
    assert blocker.args == ['RAW.PUBLIC."V_Orders"']


def test_double_click_on_a_column_inserts_only_its_name(tree: ObjectTree, qtbot) -> None:
    tree.controller._on_nodes(("RAW", "PUBLIC", "ORDERS"), COLUMNS)
    item = item_for(tree, ("RAW", "PUBLIC", "ORDERS", "order date"))
    with qtbot.waitSignal(tree.insert_requested, timeout=500) as blocker:
        tree._on_double_clicked(item, 0)
    assert blocker.args == ['"order date"']


def test_refresh_reloads_that_node(tree: ObjectTree) -> None:
    item = item_for(tree, ("RAW", "PUBLIC"))
    tree.refresh(item)
    assert tree.controller.cached(("RAW", "PUBLIC")) is None  # invalidated
    assert tree.controller.is_pending(("RAW", "PUBLIC"))


def test_filter_hides_non_matching_nodes(tree: ObjectTree) -> None:
    tree.filter_tree("v_ord")
    assert not item_for(tree, ("RAW",)).isHidden()  # kept: a descendant matches
    assert item_for(tree, ("RAW", "PUBLIC", "ORDERS")).isHidden()
    assert not item_for(tree, ("RAW", "PUBLIC", "V_Orders")).isHidden()

    tree.filter_tree("")
    assert not item_for(tree, ("RAW", "PUBLIC", "ORDERS")).isHidden()


# -- presentation (macOS conventions) ---------------------------------------


def test_the_type_column_is_consistently_capitalised(tree: ObjectTree) -> None:
    """It read "database" beside "Table" when nodes without a detail fell
    back to the raw kind string."""
    labels = [item_for(tree, p).text(1) for p in [("RAW",), ("RAW", "PUBLIC")]]
    labels += [item_for(tree, ("RAW", "PUBLIC", name)).text(1) for name in ("ORDERS", "V_Orders")]
    assert labels == ["Database", "Schema", "Table", "View"]
    assert all(label[:1].isupper() for label in labels)


def test_each_level_carries_an_icon(tree: ObjectTree) -> None:
    for path in [("RAW",), ("RAW", "PUBLIC"), ("RAW", "PUBLIC", "ORDERS")]:
        assert not item_for(tree, path).icon(0).isNull(), f"{path} has no icon"


def test_columns_have_no_icon_of_their_own(tree: ObjectTree) -> None:
    tree.controller._on_nodes(("RAW", "PUBLIC", "ORDERS"), COLUMNS)
    assert item_for(tree, ("RAW", "PUBLIC", "ORDERS", "ORDER_ID")).icon(0).isNull()


# -- errors ------------------------------------------------------------------


def test_a_failed_expand_shows_the_first_line_and_the_rest_in_a_tooltip(tree: ObjectTree) -> None:
    message = (
        "[2003] (SQLSTATE 02000) SQL compilation error:\n"
        "Schema 'RAW.PUBLIC' does not exist or not authorized.\n"
        "Query ID: 01b0-0042"
    )
    tree.controller._on_failed(("RAW", "PUBLIC"), message)
    notice = item_for(tree, ("RAW", "PUBLIC")).child(0)
    assert notice.text(0) == "[2003] (SQLSTATE 02000) SQL compilation error:"
    assert notice.toolTip(0) == message


def test_a_failure_for_a_node_no_longer_there_leaves_the_tree_alone(tree: ObjectTree) -> None:
    """A missing node is not the root: the error must not replace every database."""
    tree.controller._on_failed(("GONE", "AWAY"), "Could not load")
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == ["RAW"]
