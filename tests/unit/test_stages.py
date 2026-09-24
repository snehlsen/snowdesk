"""Stage SQL, listings, plans and transfers (docs/stage-browser.md)."""

from __future__ import annotations

import gzip
import re
import threading
from pathlib import Path

import pytest

from snowdesk.db import stages
from snowdesk.model import (
    FileStatus,
    RunStatus,
    StageFile,
    StageKind,
    StageRef,
    TransferKind,
)
from tests.fakes import STORAGE, FakeConnection, FakeProgrammingError, FakeStages

LANDING = StageRef(kind=StageKind.NAMED, name="LANDING", database="RAW", schema="PUBLIC")
USER = StageRef(kind=StageKind.USER)
EXTERNAL = StageRef(
    kind=StageKind.NAMED,
    name="S3_EXPORTS",
    database="RAW",
    schema="PUBLIC",
    internal=False,
    url="s3://bucket/exports/",
)


@pytest.fixture
def conn() -> FakeConnection:
    c = FakeConnection()
    c.stage = FakeStages(
        stages=[
            {"name": "LANDING", "database_name": "RAW", "schema_name": "PUBLIC"},
            {
                "name": "S3_EXPORTS",
                "database_name": "RAW",
                "schema_name": "PUBLIC",
                "type": "EXTERNAL",
                "url": "s3://bucket/exports/",
            },
        ]
    )
    return c


def regex_of(pattern_literal: str) -> str:
    """The regex a PATTERN literal carries, as Snowflake would read it."""
    assert pattern_literal.startswith("'") and pattern_literal.endswith("'")
    return pattern_literal[1:-1].replace("''", "'")


# -- locations ---------------------------------------------------------------


def test_locations_for_each_kind_of_stage() -> None:
    assert stages.location(LANDING) == "@RAW.PUBLIC.LANDING"
    assert stages.location(LANDING, "2026-09/") == "@RAW.PUBLIC.LANDING/2026-09/"
    assert stages.location(USER, "a/b.csv") == "@~/a/b.csv"
    table = StageRef(kind=StageKind.TABLE, name="ORDERS", database="RAW", schema="PUBLIC")
    assert stages.location(table) == "@RAW.PUBLIC.%ORDERS"


def test_a_location_with_spaces_or_quoted_names_is_quoted_whole() -> None:
    assert stages.location(LANDING, "my folder/") == "'@RAW.PUBLIC.LANDING/my folder/'"
    mixed = StageRef(kind=StageKind.NAMED, name="My Stage", database="RAW", schema="PUBLIC")
    assert stages.location(mixed) == """'@RAW.PUBLIC."My Stage"'"""


@pytest.mark.parametrize("bad", ["it's.csv", "back\\slash", "new\nline", "tab\there"])
def test_names_that_cannot_be_embedded_safely_are_refused(bad: str) -> None:
    with pytest.raises(stages.UnsafeName):
        stages.location(LANDING, bad)
    with pytest.raises(stages.UnsafeName):
        stages.file_url(f"/tmp/{bad}")


def test_a_put_source_is_escaped_for_glob() -> None:
    """The connector globs the source, so `[1]` must not become a character class."""
    url = stages.file_url("/data/report[1].csv")
    assert url == "'file:///data/report[[]1].csv'"


def test_a_get_target_is_a_directory_url() -> None:
    assert stages.file_url("/data/out", directory=True) == "'file:///data/out/'"


# -- patterns and prefixes ----------------------------------------------------


@pytest.mark.filterwarnings("ignore:Possible nested set:FutureWarning")
def test_exact_pattern_matches_the_internal_path_it_is_compared_with() -> None:
    """PATTERN is matched against `<storage>/<path from the stage root>` (§13)."""
    names = ["p/a.csv", "p/(b)+[c]{2}|$.csv"]
    literal = stages.exact_pattern(names)
    assert "\\" not in literal  # bracket escapes only
    regex = regex_of(literal)
    for name in names:
        assert re.fullmatch(regex, f"{STORAGE}/{name}"), name
    for other in ["p/a.csv.bak", "p/aXcsv", "p/a.csv/x", "q/a.csv", "pp/a.csv"]:
        assert not re.fullmatch(regex, f"{STORAGE}/{other}"), other
    # The same path further down still matches, which is why every pattern
    # is checked with LIST before it is used.
    assert re.fullmatch(regex, f"{STORAGE}/p/x/p/a.csv")


def test_a_caret_cannot_be_matched_exactly_and_is_refused() -> None:
    with pytest.raises(stages.UnsafeName):
        stages.exact_pattern(["p/a^b.csv"])


def test_removing_a_folder_needs_its_trailing_slash() -> None:
    """Without it, `REMOVE @s/data` would also remove `data2.csv`."""
    with pytest.raises(ValueError):
        stages.remove_sql(LANDING, "data")
    assert stages.remove_sql(LANDING, "data/") == "REMOVE @RAW.PUBLIC.LANDING/data/"


def test_removing_a_file_names_it_exactly() -> None:
    sql = stages.remove_sql(LANDING, "a/", ["a/x.csv"])
    assert sql == "REMOVE @RAW.PUBLIC.LANDING/a/ PATTERN='^.*/(a/x[.]csv)$'"


def test_relative_names() -> None:
    assert stages.relative_name(LANDING, "landing/2026-09/x.csv") == "2026-09/x.csv"
    assert stages.relative_name(USER, "staged/x.csv") == "staged/x.csv"
    assert stages.relative_name(EXTERNAL, "s3://bucket/exports/2026/x.csv") == "2026/x.csv"


# -- listing and folders ------------------------------------------------------


def test_show_stages_marks_external_ones(conn: FakeConnection) -> None:
    found = stages.list_stages(conn)
    assert [(s.name, s.internal) for s in found] == [("LANDING", True), ("S3_EXPORTS", False)]
    assert found[1].url == "s3://bucket/exports/"


def test_list_files_caps_and_says_so(conn: FakeConnection) -> None:
    assert conn.stage is not None
    for i in range(5):
        conn.stage.files[f"landing/f{i}.csv"] = b"x"
    files, truncated = stages.list_files(conn, LANDING, cap=3)
    assert len(files) == 3 and truncated
    files, truncated = stages.list_files(conn, LANDING, cap=10)
    assert len(files) == 5 and not truncated
    assert files[0].name == "f0.csv" and files[0].raw == "landing/f0.csv"


def test_build_tree_groups_by_slash() -> None:
    files = [
        StageFile(name="top.csv", raw="", size=1),
        StageFile(name="2026-09/a.csv", raw="", size=10),
        StageFile(name="2026-09/deep/b.csv", raw="", size=100),
    ]
    root = stages.build_tree(files)
    assert [f.name for f in root.files] == ["top.csv"]
    month = root.folders["2026-09"]
    assert month.path == "2026-09/"
    assert month.file_count == 2 and month.size == 110
    assert month.folders["deep"].path == "2026-09/deep/"

    sub = stages.build_tree(files, "2026-09/")
    assert [f.name for f in sub.files] == ["2026-09/a.csv"]


# -- statements the menu generates ---------------------------------------------


def test_copy_into_a_file_names_it_and_guesses_the_format() -> None:
    sql = stages.copy_into_sql(LANDING, "2026-09/orders.json.gz")
    assert sql.splitlines() == [
        "COPY INTO <table>",
        "FROM @RAW.PUBLIC.LANDING/2026-09/",
        "FILES = ('orders.json.gz')",
        "FILE_FORMAT = (TYPE = JSON)",
    ]


def test_copy_into_from_a_table_stage_targets_its_table() -> None:
    table = StageRef(kind=StageKind.TABLE, name="ORDERS", database="RAW", schema="PUBLIC")
    assert stages.copy_into_sql(table, "").startswith("COPY INTO RAW.PUBLIC.ORDERS\n")


def test_select_from_a_file_names_it_by_its_path() -> None:
    file = StageFile(name="2026-09/a.csv.gz", raw="landing/2026-09/a.csv.gz")
    sql = stages.select_file_sql(LANDING, file)
    assert "FROM @RAW.PUBLIC.LANDING/2026-09/a.csv.gz t" in sql
    assert "PATTERN" not in sql and "FILE_FORMAT" not in sql  # CSV needs no format
    json = StageFile(name="b.json", raw="landing/b.json")
    assert "(FILE_FORMAT => '<file_format>')" in stages.select_file_sql(LANDING, json)


# -- names on the way up --------------------------------------------------------


def test_predicted_target_follows_the_connectors_compression(tmp_path: Path) -> None:
    plain = tmp_path / "orders.csv"
    plain.write_text("a,b\n")
    assert stages.predicted_target(plain) == "orders.csv.gz"
    already = tmp_path / "orders.csv.gz"
    already.write_bytes(gzip.compress(b"a"))
    assert stages.predicted_target(already) == "orders.csv.gz"
    parquet = tmp_path / "data.bin"
    parquet.write_bytes(b"PAR1....")
    assert stages.predicted_target(parquet) == "data.bin"


# -- uploads ----------------------------------------------------------------------


def upload(conn: FakeConnection, paths: list[Path], folder: str = "", replace: bool = False):
    plan = stages.plan_upload(conn, "t1", LANDING, folder, [str(p) for p in paths])
    plan.replace = replace
    statements: list = []
    files: list = []
    summary = stages.run_transfer(
        conn,
        plan,
        threading.Event(),
        stages.Callbacks(statement=statements.append, file_done=files.append),
    )
    return plan, summary, statements, files


def test_upload_puts_each_file_and_reports_the_compressed_name(
    conn: FakeConnection, tmp_path: Path
) -> None:
    a = tmp_path / "a.csv"
    a.write_text("1\n")
    plan, summary, statements, files = upload(conn, [a], "2026-09/")
    assert plan.conflicts == []
    assert summary.counts == {FileStatus.UPLOADED: 1}
    assert [f.name for f in files] == ["2026-09/a.csv.gz"]
    assert conn.stage is not None and "landing/2026-09/a.csv.gz" in conn.stage.files
    assert len(statements) == 1
    assert statements[0].statement.is_put_or_get
    assert statements[0].status is RunStatus.SUCCESS
    assert "OVERWRITE=FALSE" in statements[0].statement.sql


def test_a_dropped_folder_keeps_its_structure_and_skips_hidden_files(
    conn: FakeConnection, tmp_path: Path
) -> None:
    drop = tmp_path / "batch"
    (drop / "sub").mkdir(parents=True)
    (drop / "one.csv").write_text("1")
    (drop / "sub" / "two.csv").write_text("2")
    (drop / ".DS_Store").write_text("junk")
    _plan, summary, _s, _f = upload(conn, [drop])
    assert summary.counts == {FileStatus.UPLOADED: 2}
    assert conn.stage is not None
    assert sorted(conn.stage.files) == ["landing/batch/one.csv.gz", "landing/batch/sub/two.csv.gz"]


def test_an_existing_file_is_a_conflict_and_is_skipped_unless_replaced(
    conn: FakeConnection, tmp_path: Path
) -> None:
    assert conn.stage is not None
    conn.stage.files["landing/a.csv.gz"] = b"old"
    a = tmp_path / "a.csv"
    a.write_text("new")

    plan, summary, statements, _f = upload(conn, [a])
    assert plan.conflicts == ["a.csv.gz"]
    assert summary.counts == {FileStatus.SKIPPED: 1}
    assert statements == []  # nothing ran
    assert conn.stage.files["landing/a.csv.gz"] == b"old"

    _plan, summary, statements, _f = upload(conn, [a], replace=True)
    assert summary.counts == {FileStatus.UPLOADED: 1}
    assert "OVERWRITE=TRUE" in statements[0].statement.sql
    assert gzip.decompress(conn.stage.files["landing/a.csv.gz"]) == b"new"


def test_an_unsafe_local_name_is_refused_and_the_rest_still_upload(
    conn: FakeConnection, tmp_path: Path
) -> None:
    good = tmp_path / "good.csv"
    good.write_text("1")
    bad = tmp_path / "it's.csv"
    bad.write_text("2")
    plan, summary, _s, files = upload(conn, [good, bad])
    assert [name for name, _why in plan.refused] == [str(bad)]
    assert summary.counts == {FileStatus.UPLOADED: 1, FileStatus.FAILED: 1}
    assert any(f.status is FileStatus.FAILED and "quote" in f.detail for f in files)


def test_stop_takes_effect_between_files(conn: FakeConnection, tmp_path: Path) -> None:
    paths = []
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.csv"
        path.write_text(name)
        paths.append(str(path))
    stop = threading.Event()
    assert conn.stage is not None
    # Stop is pressed while the first PUT is running.
    conn.stage.before_transfer = lambda n: stop.set() if n == 1 else None
    plan = stages.plan_upload(conn, "t1", LANDING, "", paths)
    summary = stages.run_transfer(conn, plan, stop)
    assert summary.stopped
    assert summary.counts == {FileStatus.UPLOADED: 1, FileStatus.NOT_STARTED: 2}
    assert list(conn.stage.files) == ["landing/a.csv.gz"]


def test_a_failed_put_does_not_stop_the_others(conn: FakeConnection, tmp_path: Path) -> None:
    paths = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.csv"
        path.write_text(name)
        paths.append(path)
    assert conn.stage is not None
    conn.stage.fail_on[1] = FakeProgrammingError("Insufficient privileges", errno=3001)
    _plan, summary, statements, _f = upload(conn, paths)
    assert summary.counts == {FileStatus.FAILED: 1, FileStatus.UPLOADED: 1}
    assert [s.status for s in statements] == [RunStatus.ERROR, RunStatus.SUCCESS]
    assert not summary.error


def test_a_lost_session_ends_the_transfer(conn: FakeConnection, tmp_path: Path) -> None:
    paths = []
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.csv"
        path.write_text(name)
        paths.append(path)
    assert conn.stage is not None
    conn.stage.fail_on[1] = FakeProgrammingError("Session no longer exists.", errno=390111)
    _plan, summary, _s, _f = upload(conn, paths)
    assert summary.error
    assert summary.counts == {FileStatus.FAILED: 1, FileStatus.NOT_STARTED: 2}
    assert conn.stage.transfers == 1


def test_uploading_to_an_external_stage_is_refused(conn: FakeConnection, tmp_path: Path) -> None:
    with pytest.raises(stages.UnsafeName):
        stages.plan_upload(conn, "t1", EXTERNAL, "", [str(tmp_path)])


# -- downloads ---------------------------------------------------------------------


def download(conn: FakeConnection, selection: list[str], root: Path, replace: bool = False):
    plan = stages.plan_download(conn, "t1", LANDING, selection, root)
    plan.replace = replace
    statements: list = []
    summary = stages.run_transfer(
        conn, plan, threading.Event(), stages.Callbacks(statement=statements.append)
    )
    return plan, summary, statements


def test_a_folder_download_keeps_the_stage_structure(conn: FakeConnection, tmp_path: Path) -> None:
    """GET alone would put both x.csv files in one place, one over the other."""
    assert conn.stage is not None
    conn.stage.files.update(
        {
            "landing/2026/a/x.csv": b"from a",
            "landing/2026/b/x.csv": b"from b",
            "landing/2026/top.csv": b"top",
            "landing/2027/other.csv": b"not selected",
        }
    )
    _plan, summary, statements = download(conn, ["2026/"], tmp_path)
    assert summary.counts == {FileStatus.DOWNLOADED: 3}
    assert (tmp_path / "2026" / "a" / "x.csv").read_bytes() == b"from a"
    assert (tmp_path / "2026" / "b" / "x.csv").read_bytes() == b"from b"
    assert (tmp_path / "2026" / "top.csv").read_bytes() == b"top"
    assert not (tmp_path / "2027").exists()
    assert len(statements) == 3  # one GET per stage folder


def test_a_single_file_download_does_not_take_its_prefix_siblings(
    conn: FakeConnection, tmp_path: Path
) -> None:
    assert conn.stage is not None
    conn.stage.files.update({"landing/data.csv": b"one", "landing/data.csv.bak": b"two"})
    _plan, summary, _s = download(conn, ["data.csv"], tmp_path)
    assert summary.counts == {FileStatus.DOWNLOADED: 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.csv"]


def test_local_files_in_the_way_are_conflicts(conn: FakeConnection, tmp_path: Path) -> None:
    assert conn.stage is not None
    conn.stage.files["landing/a.csv"] = b"remote"
    (tmp_path / "a.csv").write_bytes(b"local")
    plan, summary, _s = download(conn, ["a.csv"], tmp_path)
    assert plan.conflicts == [str(tmp_path / "a.csv")]
    assert summary.counts == {FileStatus.SKIPPED: 1}
    assert (tmp_path / "a.csv").read_bytes() == b"local"

    _plan, summary, _s = download(conn, ["a.csv"], tmp_path, replace=True)
    assert (tmp_path / "a.csv").read_bytes() == b"remote"


@pytest.mark.parametrize("evil", ["landing/../../escape.txt", "landing/a/../../../escape.txt"])
def test_a_stage_path_cannot_write_outside_the_chosen_folder(
    conn: FakeConnection, tmp_path: Path, evil: str
) -> None:
    assert conn.stage is not None
    conn.stage.files[evil] = b"gotcha"
    root = tmp_path / "downloads"
    root.mkdir()
    plan, summary, statements = download(conn, [""], root)
    assert plan.downloads == []
    assert len(plan.refused) == 1
    assert summary.counts == {FileStatus.FAILED: 1}
    assert statements == []
    assert not (tmp_path / "escape.txt").exists()


# -- removes -------------------------------------------------------------------------


def remove(conn: FakeConnection, selection: list[StageFile]):
    plan = stages.plan_remove("t1", LANDING, selection)
    statements: list = []
    summary = stages.run_transfer(
        conn, plan, threading.Event(), stages.Callbacks(statement=statements.append)
    )
    return summary, statements


def test_removing_a_file_leaves_names_that_merely_start_the_same(conn: FakeConnection) -> None:
    assert conn.stage is not None
    conn.stage.files.update({"landing/data.csv": b"1", "landing/data.csv.bak": b"2"})
    summary, _s = remove(conn, [StageFile(name="data.csv", raw="landing/data.csv")])
    assert summary.kind is TransferKind.REMOVE
    assert summary.counts == {FileStatus.REMOVED: 1}
    assert list(conn.stage.files) == ["landing/data.csv.bak"]


def test_removing_a_folder_leaves_its_prefix_siblings(conn: FakeConnection) -> None:
    assert conn.stage is not None
    conn.stage.files.update(
        {"landing/a/1.csv": b"1", "landing/a/deep/2.csv": b"2", "landing/ab/3.csv": b"3"}
    )
    summary, statements = remove(
        conn,
        [
            StageFile(name="a/", raw=""),
            # Already covered by the folder; must not cost a statement.
            StageFile(name="a/1.csv", raw="landing/a/1.csv"),
        ],
    )
    assert summary.counts == {FileStatus.REMOVED: 1}
    assert len(statements) == 1
    assert list(conn.stage.files) == ["landing/ab/3.csv"]


def test_a_remove_of_a_file_already_gone_runs_nothing(conn: FakeConnection) -> None:
    summary, statements = remove(conn, [StageFile(name="gone.csv", raw="landing/gone.csv")])
    assert summary.counts == {FileStatus.FAILED: 1}
    assert statements == []


# -- the same path further down (§13) --------------------------------------------


def test_removing_a_file_spares_the_same_path_further_down(conn: FakeConnection) -> None:
    """`.*/(p/a.csv)` also matches p/x/p/a.csv; addressing the file alone does not."""
    assert conn.stage is not None
    conn.stage.files.update({"landing/p/a.csv": b"1", "landing/p/x/p/a.csv": b"2"})
    summary, statements = remove(conn, [StageFile(name="p/a.csv", raw="landing/p/a.csv")])
    assert summary.counts == {FileStatus.REMOVED: 1}
    assert [s.statement.sql for s in statements] == [
        "REMOVE @RAW.PUBLIC.LANDING/p/a.csv PATTERN='^.*/(p/a[.]csv)$'"
    ]
    assert list(conn.stage.files) == ["landing/p/x/p/a.csv"]


def test_removing_a_root_file_spares_its_namesakes_in_folders(conn: FakeConnection) -> None:
    assert conn.stage is not None
    conn.stage.files.update({"landing/data.csv": b"1", "landing/2026/data.csv": b"2"})
    summary, _s = remove(conn, [StageFile(name="data.csv", raw="landing/data.csv")])
    assert summary.counts == {FileStatus.REMOVED: 1}
    assert list(conn.stage.files) == ["landing/2026/data.csv"]


def test_a_file_that_cannot_be_matched_alone_is_left_alone(conn: FakeConnection) -> None:
    """Even its own path as the location still matches a folder named like it."""
    assert conn.stage is not None
    conn.stage.files.update(
        {"landing/p/a.csv": b"1", "landing/p/a.csv/p/a.csv": b"2", "landing/p/b.csv": b"3"}
    )
    summary, _s = remove(
        conn,
        [
            StageFile(name="p/a.csv", raw="landing/p/a.csv"),
            StageFile(name="p/b.csv", raw="landing/p/b.csv"),
        ],
    )
    assert summary.counts == {FileStatus.FAILED: 1, FileStatus.REMOVED: 1}
    assert sorted(conn.stage.files) == ["landing/p/a.csv", "landing/p/a.csv/p/a.csv"]


def test_downloading_a_root_file_does_not_take_its_namesakes(
    conn: FakeConnection, tmp_path: Path
) -> None:
    assert conn.stage is not None
    conn.stage.files.update({"landing/data.csv": b"root", "landing/2026/data.csv": b"deeper"})
    _plan, summary, statements = download(conn, ["data.csv"], tmp_path)
    assert summary.counts == {FileStatus.DOWNLOADED: 1}
    assert (tmp_path / "data.csv").read_bytes() == b"root"
    assert [s.statement.sql.split(" PATTERN")[0] for s in statements] == [
        f"GET @RAW.PUBLIC.LANDING/data.csv 'file://{tmp_path}/'"
    ]
