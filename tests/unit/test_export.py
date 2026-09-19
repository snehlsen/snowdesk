from __future__ import annotations

import io

from snowdesk.model import ColumnInfo
from snowdesk.util.export import rows_to_tsv, write_csv

COLUMNS = [ColumnInfo("ID", "FIXED", 38, 0), ColumnInfo("NAME", "TEXT")]
ROWS = [(1, "alice"), (2, None), (3, "with\ttab")]


def test_tsv_without_headers() -> None:
    assert rows_to_tsv(ROWS, COLUMNS) == "1\talice\n2\tNULL\n3\twith\\ttab"


def test_tsv_with_headers() -> None:
    assert rows_to_tsv(ROWS[:1], COLUMNS, with_headers=True) == "ID\tNAME\n1\talice"


def test_tsv_column_subset_keeps_order() -> None:
    assert rows_to_tsv(ROWS[:1], COLUMNS, with_headers=True, column_indexes=[1]) == "NAME\nalice"


def test_tsv_escapes_newlines() -> None:
    assert rows_to_tsv([(1, "a\nb")], COLUMNS) == "1\ta\\nb"


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
