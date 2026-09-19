from __future__ import annotations

from pathlib import Path

from snowdesk.model import RunStatus, Statement, StatementOutcome
from snowdesk.storage.history import HistoryStore


def outcome(
    sql: str, status: RunStatus = RunStatus.SUCCESS, rows: int | None = 3
) -> StatementOutcome:
    return StatementOutcome(
        index=0,
        statement=Statement(sql=sql, start=0, end=len(sql)),
        status=status,
        query_id="01b0-0001",
        duration_s=1.5,
        row_count=rows,
        message="ok",
    )


def test_records_and_reads_back(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("select 1"), "dev")
    entries = store.recent()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.sql == "select 1"
    assert entry.connection == "dev"
    assert entry.status == "success"
    assert entry.row_count == 3
    assert entry.query_id == "01b0-0001"
    store.close()


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    first = HistoryStore(path)
    first.record(outcome("select 1"), "dev")
    first.close()
    second = HistoryStore(path)
    assert [e.sql for e in second.recent()] == ["select 1"]
    second.close()


def test_skipped_statements_are_not_recorded(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("select 1", RunStatus.SKIPPED), "dev")
    assert store.recent() == []
    store.close()


def test_errors_and_cancellations_are_recorded(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("bad sql", RunStatus.ERROR, None), "dev")
    store.record(outcome("select system$wait(60)", RunStatus.CANCELLED, None), "dev")
    assert {e.status for e in store.recent()} == {"error", "cancelled"}
    store.close()


def test_search_finds_by_substring(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("select * from orders"), "dev")
    store.record(outcome("select * from users"), "dev")
    assert [e.sql for e in store.search("orders")] == ["select * from orders"]
    assert len(store.search("")) == 2
    store.close()


def test_search_treats_wildcards_literally(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("select 100%"), "dev")
    store.record(outcome("select 1"), "dev")
    assert [e.sql for e in store.search("100%")] == ["select 100%"]
    store.close()


def test_clear_empties_the_store(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(outcome("select 1"), "dev")
    store.clear()
    assert store.recent() == []
    store.close()


def test_recent_is_newest_first(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    for i in range(3):
        store.record(outcome(f"select {i}"), "dev")
    assert store.recent()[0].sql == "select 2"
    store.close()
