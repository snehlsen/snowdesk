from __future__ import annotations

import datetime as dt
from decimal import Decimal

from snowdesk.model import ColumnInfo, columns_from_description
from snowdesk.util.formatting import (
    NULL_TEXT,
    format_cell,
    format_duration,
    format_row_count,
    pretty_json,
)

TEXT = ColumnInfo("S", "TEXT")
NUM = ColumnInfo("N", "FIXED", precision=38, scale=2)
VARIANT = ColumnInfo("V", "VARIANT")
TS_TZ = ColumnInfo("T", "TIMESTAMP_TZ")


def test_null_is_distinct_from_empty_string() -> None:
    assert format_cell(None, TEXT) == NULL_TEXT
    assert format_cell("", TEXT) == ""
    assert format_cell("NULL", TEXT) == "NULL"  # the literal text is unchanged


def test_numbers() -> None:
    assert format_cell(Decimal("42.50"), NUM) == "42.50"
    assert format_cell(Decimal("1E+2"), NUM) == "100"
    assert format_cell(3.5, ColumnInfo("F", "REAL")) == "3.5"


def test_booleans_render_lowercase() -> None:
    assert format_cell(True, ColumnInfo("B", "BOOLEAN")) == "true"
    assert format_cell(False, ColumnInfo("B", "BOOLEAN")) == "false"


def test_timestamp_keeps_time_zone() -> None:
    value = dt.datetime(2026, 9, 10, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert format_cell(value, TS_TZ) == "2026-09-10 12:30:00.000000+02:00"


def test_naive_timestamp_has_no_offset() -> None:
    value = dt.datetime(2026, 9, 10, 12, 30)
    assert format_cell(value, ColumnInfo("T", "TIMESTAMP_NTZ")) == "2026-09-10 12:30:00.000000"


def test_date_and_time() -> None:
    assert format_cell(dt.date(2026, 9, 10), ColumnInfo("D", "DATE")) == "2026-09-10"
    assert format_cell(dt.time(8, 5, 1), ColumnInfo("T", "TIME")) == "08:05:01"


def test_variant_renders_compact_json() -> None:
    assert format_cell('{\n  "sku": "A1"\n}', VARIANT) == '{"sku":"A1"}'
    assert format_cell({"b": 1, "a": [1, 2]}, VARIANT) == '{"b":1,"a":[1,2]}'


def test_variant_that_is_not_json_is_left_alone() -> None:
    assert format_cell("not json", VARIANT) == "not json"


def test_binary_renders_as_hex() -> None:
    assert format_cell(b"\x00\xff", ColumnInfo("B", "BINARY")) == "00FF"


def test_type_hints() -> None:
    assert NUM.type_hint == "NUMBER(38,2)"
    assert TEXT.type_hint == "VARCHAR"
    assert ColumnInfo("F", "REAL").type_hint == "FLOAT"
    assert VARIANT.type_hint == "VARIANT"


def test_columns_from_tuple_description() -> None:
    description = [
        ("ORDER_ID", 0, None, None, 38, 0, False),
        ("PAYLOAD", 5, None, None, None, None, True),
    ]
    columns = columns_from_description(description)
    assert [c.name for c in columns] == ["ORDER_ID", "PAYLOAD"]
    assert columns[0].type_hint == "NUMBER(38,0)"
    assert columns[1].is_json


def test_row_count_text() -> None:
    assert format_row_count(500, None, False) == "500 of ? rows"
    assert format_row_count(500, 12340, False) == "500 of 12,340 rows"
    assert format_row_count(12340, 12340, True) == "12,340 rows"
    assert format_row_count(1, 1, True) == "1 row"


def test_duration_text() -> None:
    assert format_duration(0.25) == "250 ms"
    assert format_duration(1.837) == "1.84 s"
    assert format_duration(75).startswith("1m")


def test_pretty_json_indents() -> None:
    assert pretty_json('{"a":1}') == '{\n  "a": 1\n}'
