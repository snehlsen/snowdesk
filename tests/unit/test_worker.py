"""Worker job handling, driven synchronously with a fake connection."""

from __future__ import annotations

import pytest

from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.splitter import split_sql
from snowdesk.db.worker import (
    BrowseJob,
    ConnectJob,
    DisconnectJob,
    EndTransactionJob,
    FetchMoreJob,
    ProfileJob,
    RunScriptJob,
    SetAutocommitJob,
    SnowflakeWorker,
)
from snowdesk.model import RunStatus, TransactionState
from tests.fakes import FakeConnection, FakeProgrammingError, FakeStatement

COLS = [("N", 0, None, None, 38, 0, False)]


@pytest.fixture
def worker_and_conn(qapp, request):
    plan = getattr(request, "param", None) or {}
    conn = FakeConnection(plan)
    session = SnowflakeSession(connect_fn=lambda _p: conn)
    worker = SnowflakeWorker(session=session)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    return worker, conn


def collect(signal) -> list:
    received: list = []
    signal.connect(lambda *args: received.append(args if len(args) > 1 else args[0]))
    return received


def test_connect_emits_state_and_context(qapp) -> None:
    conn = FakeConnection()
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    states = collect(worker.state_changed)
    contexts = collect(worker.context_changed)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    assert [s[0] for s in states] == ["connecting", "connected"]
    assert contexts[-1].role == "ANALYST"


def test_connect_failure_emits_error(qapp) -> None:
    def boom(_p):
        raise FakeProgrammingError("bad password", errno=390100)

    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=boom))
    failures = collect(worker.connect_failed)
    states = collect(worker.state_changed)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    assert failures[0].errno == 390100
    assert states[-1][0] == "error"


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"select": FakeStatement(columns=COLS, rows=[(i,) for i in range(1200)])}],
    indirect=True,
)
def test_run_emits_first_page_then_fetches_more(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    results = collect(worker.result_ready)
    appended = collect(worker.rows_appended)

    worker._dispatch(RunScriptJob(statements=split_sql("select * from t"), page_size=500))
    result_id, columns, rows, exhausted, total, _qid, _label = results[0]
    assert len(rows) == 500
    assert not exhausted
    assert [c.name for c in columns] == ["N"]
    assert total == 1200

    worker._dispatch(FetchMoreJob(result_id=result_id, page_size=500))
    worker._dispatch(FetchMoreJob(result_id=result_id, page_size=500))
    assert [len(a[1]) for a in appended] == [500, 200]
    assert appended[-1][2] is True  # exhausted


# Snowflake hands DML and DDL results back through RESULT_SCAN, so they arrive
# as one-row result sets rather than as bare status codes.
INSERT_COLS = [("number of rows inserted", 0, None, None, 38, 0, True)]
STATUS_COLS = [("status", 2, None, None, None, None, True)]


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"insert": FakeStatement(columns=INSERT_COLS, rows=[(7,)])}],
    indirect=True,
)
def test_dml_reports_rows_affected(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    outcomes = collect(worker.statement_finished)
    results = collect(worker.result_ready)
    worker._dispatch(RunScriptJob(statements=split_sql("insert into t values (1)")))
    assert outcomes[0].status is RunStatus.SUCCESS
    assert "7 rows inserted" in outcomes[0].message
    assert outcomes[0].row_count == 7
    assert outcomes[0].result_id is None
    assert results == []  # a counter row does not deserve a result tab


@pytest.mark.parametrize(
    "worker_and_conn",
    [
        {
            "create table": FakeStatement(
                columns=STATUS_COLS, rows=[("Table T successfully created.",)]
            )
        }
    ],
    indirect=True,
)
def test_ddl_reports_its_status_line(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    outcomes = collect(worker.statement_finished)
    results = collect(worker.result_ready)
    worker._dispatch(RunScriptJob(statements=split_sql("create table t (a int)")))
    assert "Table T successfully created." in outcomes[0].message
    assert results == []


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"select": FakeStatement(columns=[("N", 0, None, None, 38, 0, False)], rows=[(1,)])}],
    indirect=True,
)
def test_select_opens_a_result_tab(worker_and_conn) -> None:
    """The async cursor only reveals its columns once rows are pulled."""
    worker, _conn = worker_and_conn
    outcomes = collect(worker.statement_finished)
    results = collect(worker.result_ready)
    worker._dispatch(RunScriptJob(statements=split_sql("select 1 as n")))
    assert len(results) == 1
    result_id, columns, rows, exhausted, _total, _qid, _label = results[0]
    assert [c.name for c in columns] == ["N"]
    assert rows == [(1,)]
    assert exhausted
    assert outcomes[0].result_id == result_id


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"select": FakeStatement(columns=STATUS_COLS, rows=[("shipped",)])}],
    indirect=True,
)
def test_a_query_returning_one_status_column_still_gets_a_grid(worker_and_conn) -> None:
    """`select status from t` has the exact shape of a DDL result."""
    worker, _conn = worker_and_conn
    results = collect(worker.result_ready)
    worker._dispatch(RunScriptJob(statements=split_sql("select status from orders limit 1")))
    assert len(results) == 1


@pytest.mark.parametrize(
    "worker_and_conn",
    [
        {
            "boom": FakeStatement(
                error=FakeProgrammingError(
                    "Object BOOM does not exist", errno=2003, sqlstate="42S02"
                )
            )
        }
    ],
    indirect=True,
)
def test_run_stops_at_the_first_error(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    outcomes = collect(worker.statement_finished)
    worker._dispatch(RunScriptJob(statements=split_sql("select 1; select boom; select 3;")))
    assert [o.status for o in outcomes] == [
        RunStatus.SUCCESS,
        RunStatus.ERROR,
        RunStatus.SKIPPED,
    ]
    assert outcomes[1].error is not None
    assert outcomes[1].error.errno == 2003


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"use role": FakeStatement(rowcount=1)}],
    indirect=True,
)
def test_use_statements_run_in_order_and_refresh_context(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    contexts = collect(worker.context_changed)
    script = "use role ANALYST;\nuse warehouse COMPUTE_WH;\nselect 1;"
    worker._dispatch(RunScriptJob(statements=split_sql(script)))
    assert conn.executed[-3:] == ["use role ANALYST", "use warehouse COMPUTE_WH", "select 1"]
    assert contexts[-1].role == "ANALYST"


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"wait": FakeStatement(columns=COLS, rows=[(1,)], polls=1000)}],
    indirect=True,
)
def test_cancel_marks_the_statement_cancelled(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    outcomes = collect(worker.statement_finished)

    # Cancel as soon as Snowflake hands back a query id.
    worker.statement_started.connect(lambda *_: worker.cancel_running())
    worker._dispatch(RunScriptJob(statements=split_sql("select system$wait(60) as wait")))

    assert outcomes[0].status is RunStatus.CANCELLED
    assert outcomes[0].query_id is not None


def test_browse_walks_one_level_per_job(qapp) -> None:
    conn = FakeConnection(
        {
            "SHOW DATABASES": FakeStatement(
                columns=[("name", 2, None, None, None, None, True)], rows=[("RAW",), ("ANALYTICS",)]
            ),
            "SHOW SCHEMAS": FakeStatement(
                columns=[("name", 2, None, None, None, None, True)],
                rows=[("PUBLIC",), ("INFORMATION_SCHEMA",)],
            ),
            "SHOW OBJECTS": FakeStatement(
                columns=[
                    ("name", 2, None, None, None, None, True),
                    ("kind", 2, None, None, None, None, True),
                ],
                rows=[("ORDERS", "TABLE"), ("V_ORDERS", "VIEW")],
            ),
        }
    )
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    received = collect(worker.nodes_ready)

    worker._dispatch(BrowseJob(path=()))
    assert [n.name for n in received[-1][1]] == ["RAW", "ANALYTICS"]

    worker._dispatch(BrowseJob(path=("RAW",)))
    assert [n.name for n in received[-1][1]] == ["PUBLIC"]  # INFORMATION_SCHEMA hidden

    worker._dispatch(BrowseJob(path=("RAW", "PUBLIC")))
    nodes = received[-1][1]
    assert [(n.name, n.kind) for n in nodes] == [("ORDERS", "table"), ("V_ORDERS", "view")]


def test_run_without_connection_is_reported(qapp) -> None:
    worker = SnowflakeWorker()
    errors = collect(worker.worker_error)
    worker._dispatch(RunScriptJob(statements=split_sql("select 1")))
    assert errors == ["Not connected."]


# --------------------------------------------------------------------------
# Query profile
# --------------------------------------------------------------------------

PROFILE_COLS = [
    ("STEP_ID", 0, None, None, 38, 0, False),
    ("OPERATOR_ID", 0, None, None, 38, 0, False),
    ("OPERATOR_TYPE", 2, None, None, None, None, False),
]
QID = "01b8e0f5-0000-d7a5-0000-a3bd0003e0ba"


@pytest.mark.parametrize(
    "worker_and_conn",
    [
        {
            "GET_QUERY_OPERATOR_STATS": FakeStatement(
                columns=PROFILE_COLS, rows=[(1, 0, "Result"), (1, 1, "TableScan")]
            )
        }
    ],
    indirect=True,
)
def test_profile_opens_a_labelled_tab_for_the_profiled_query(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    results = collect(worker.result_ready)

    worker._dispatch(ProfileJob(query_id=QID))

    assert f"GET_QUERY_OPERATOR_STATS('{QID}')" in conn.executed[-1]
    _result_id, columns, rows, exhausted, _total, query_id, label = results[0]
    assert [c.name for c in columns] == ["STEP_ID", "OPERATOR_ID", "OPERATOR_TYPE"]
    assert len(rows) == 2
    assert exhausted
    # The tab carries the profiled query, not the stats call that filled it.
    assert query_id == QID
    assert label == "Profile · 01b8e0f5"


@pytest.mark.parametrize(
    "worker_and_conn",
    [{"GET_QUERY_OPERATOR_STATS": FakeStatement(columns=PROFILE_COLS, rows=[])}],
    indirect=True,
)
def test_profile_without_operator_stats_explains_itself(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    results = collect(worker.result_ready)
    empty = collect(worker.profile_empty)

    worker._dispatch(ProfileJob(query_id=QID))

    assert empty == [QID]
    assert results == []  # an empty grid would say nothing about why
    assert len(worker.results) == 0  # and its cursor is not left open


@pytest.mark.parametrize(
    "worker_and_conn",
    [
        {
            "GET_QUERY_OPERATOR_STATS": FakeStatement(
                error=FakeProgrammingError("Query not found", errno=709)
            )
        }
    ],
    indirect=True,
)
def test_profile_failure_is_reported_against_the_query_id(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    failures = collect(worker.profile_failed)
    worker._dispatch(ProfileJob(query_id=QID))
    assert failures[0][0] == QID
    assert "Query not found" in failures[0][1]


def test_profile_rejects_a_value_that_is_not_a_query_id(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    failures = collect(worker.profile_failed)
    worker._dispatch(ProfileJob(query_id="select 1"))
    assert "not a Snowflake query id" in failures[0][1]
    assert not any("GET_QUERY_OPERATOR_STATS" in sql for sql in conn.executed)


def test_profile_without_a_connection_says_so(qapp) -> None:
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: FakeConnection()))
    failures = collect(worker.profile_failed)
    worker._dispatch(ProfileJob(query_id=QID))
    assert failures[0] == (QID, "Not connected.")


# -- commit mode and transactions (Q10) ---------------------------------------


def run_sql(worker: SnowflakeWorker, sql: str) -> None:
    worker._dispatch(RunScriptJob(statements=split_sql(sql)))


def test_connect_reads_the_commit_mode(qapp) -> None:
    conn = FakeConnection()
    conn.autocommit = False  # as connections.toml can set it
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    states = collect(worker.transaction_changed)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    assert states[-1] == TransactionState(autocommit=False, transaction_id=None)


def test_switching_to_manual_commit(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    states = collect(worker.transaction_changed)
    worker._dispatch(SetAutocommitJob(enabled=False))
    assert conn.executed[-1] == "ALTER SESSION SET AUTOCOMMIT = FALSE"
    assert states[-1].autocommit is False


def test_dml_in_manual_mode_opens_a_transaction_and_commit_ends_it(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    worker._dispatch(SetAutocommitJob(enabled=False))
    states = collect(worker.transaction_changed)
    ended = collect(worker.transaction_ended)

    run_sql(worker, "insert into t values (1)")
    assert states[-1].in_transaction
    assert states[-1].transaction_id == conn.transaction_id

    worker._dispatch(EndTransactionJob(commit=True))
    assert conn.executed[-1] == "COMMIT"
    assert ended[-1].status is RunStatus.SUCCESS
    assert ended[-1].statement.sql == "COMMIT"
    assert not states[-1].in_transaction
    assert states[-1].autocommit is False


def test_rollback_ends_the_transaction(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    run_sql(worker, "begin")
    ended = collect(worker.transaction_ended)
    worker._dispatch(EndTransactionJob(commit=False))
    assert conn.executed[-1] == "ROLLBACK"
    assert ended[-1].message.startswith("Rolled back")
    assert conn.transaction_id is None


def test_an_explicit_begin_shows_even_with_autocommit_on(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    states = collect(worker.transaction_changed)
    run_sql(worker, "begin; insert into t values (1)")
    assert states[-1].autocommit is True
    assert states[-1].in_transaction


def test_ddl_commits_implicitly(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    worker._dispatch(SetAutocommitJob(enabled=False))
    states = collect(worker.transaction_changed)
    run_sql(worker, "insert into t values (1)")
    assert states[-1].in_transaction
    run_sql(worker, "create table u (n int)")
    assert not states[-1].in_transaction


def test_the_mode_does_not_change_mid_transaction(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    worker._dispatch(SetAutocommitJob(enabled=False))
    run_sql(worker, "insert into t values (1)")
    failures = collect(worker.autocommit_failed)

    worker._dispatch(SetAutocommitJob(enabled=True))
    assert "transaction is open" in failures[-1]
    assert "ALTER SESSION SET AUTOCOMMIT = TRUE" not in conn.executed
    assert conn.autocommit is False


def test_a_script_that_sets_autocommit_is_read_back(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    states = collect(worker.transaction_changed)
    run_sql(worker, "alter session set autocommit = false")
    assert conn.status_queries[-2].startswith("SHOW PARAMETERS LIKE 'AUTOCOMMIT'")
    assert states[-1].autocommit is False


def test_autocommit_is_only_re_read_when_a_script_mentions_it(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    before = len(conn.status_queries)
    run_sql(worker, "select 1; select 2")
    asked = conn.status_queries[before:]
    assert asked == ["SELECT CURRENT_TRANSACTION()"]


def test_a_failed_commit_is_reported(qapp) -> None:
    conn = FakeConnection(
        {"commit": FakeStatement(error=FakeProgrammingError("Commit refused", errno=1234))}
    )
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    run_sql(worker, "begin")
    ended = collect(worker.transaction_ended)
    states = collect(worker.transaction_changed)
    worker._dispatch(EndTransactionJob(commit=True))
    assert ended[-1].status is RunStatus.ERROR
    assert "[1234]" in ended[-1].message
    assert states[-1].in_transaction  # still open, and still shown


def test_losing_the_session_while_reading_the_transaction_resets_it(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    run_sql(worker, "begin")
    lost = collect(worker.connection_lost)
    states = collect(worker.transaction_changed)
    conn.status_error = FakeProgrammingError("Session no longer exists", errno=390104)
    run_sql(worker, "select 1")
    assert lost
    assert states[-1] == TransactionState()
    assert not worker.session.is_connected


def test_an_unreadable_transaction_keeps_what_was_last_known(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    run_sql(worker, "begin")
    states = collect(worker.transaction_changed)
    conn.status_error = FakeProgrammingError("SQL compilation error", errno=1003)
    run_sql(worker, "select 1")
    assert states[-1].in_transaction
    assert worker.session.is_connected


def test_disconnect_clears_the_transaction_state(worker_and_conn) -> None:
    worker, _conn = worker_and_conn
    states = collect(worker.transaction_changed)
    worker._dispatch(DisconnectJob())
    assert states[-1] == TransactionState()


def _size_lookups(conn: FakeConnection) -> list[str]:
    return [q for q in conn.status_queries if q.startswith("SHOW WAREHOUSES")]


def test_connect_reads_the_warehouse_size(qapp) -> None:
    conn = FakeConnection()
    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=lambda _p: conn))
    contexts = collect(worker.context_changed)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    assert contexts[-1].warehouse_size == "X-Small"
    assert str(contexts[-1]) == "ANALYST · COMPUTE_WH (X-Small) · RAW.PUBLIC"


def test_the_warehouse_size_is_only_re_read_when_it_may_have_changed(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    contexts = collect(worker.context_changed)
    before = len(_size_lookups(conn))
    run_sql(worker, "select 1")
    assert len(_size_lookups(conn)) == before

    conn.warehouses["COMPUTE_WH"] = "Large"
    run_sql(worker, "alter warehouse compute_wh set warehouse_size = large")
    assert contexts[-1].warehouse_size == "Large"

    conn.warehouses["BIG_WH"] = "4X-Large"
    conn.warehouse = "BIG_WH"
    run_sql(worker, "select 1")
    assert contexts[-1].warehouse_size == "4X-Large"


def test_a_warehouse_the_role_cannot_see_has_no_size(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    contexts = collect(worker.context_changed)
    conn.warehouse = "HIDDEN_WH"
    run_sql(worker, "select 1")
    assert contexts[-1].warehouse_size is None
    assert str(contexts[-1]) == "ANALYST · HIDDEN_WH · RAW.PUBLIC"


def test_the_size_matches_a_warehouse_named_in_lower_case(worker_and_conn) -> None:
    worker, conn = worker_and_conn
    contexts = collect(worker.context_changed)
    conn.warehouse = "compute_wh"
    run_sql(worker, "select 1")
    assert contexts[-1].warehouse_size == "X-Small"
