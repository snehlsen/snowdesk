"""The editor session file (E2)."""

from __future__ import annotations

import json
from pathlib import Path

from snowdesk.storage.session import (
    MAX_BUFFER_CHARS,
    SessionState,
    SessionStore,
    TabState,
)


def test_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    state = SessionState(
        tabs=[
            TabState(title="Untitled 1", text="select 1", cursor=5),
            TabState(title="q.sql", path="/tmp/q.sql", cursor=0),
        ],
        current=1,
    )
    assert store.save(state)
    loaded = store.load()
    assert [t.title for t in loaded.tabs] == ["Untitled 1", "q.sql"]
    assert loaded.tabs[0].text == "select 1"
    assert loaded.tabs[0].cursor == 5
    assert loaded.tabs[1].path == "/tmp/q.sql"
    assert loaded.tabs[1].text is None  # clean file tabs re-read from disk
    assert loaded.current == 1


def test_missing_file_is_an_empty_session(tmp_path: Path) -> None:
    assert SessionStore(tmp_path / "nope.json").load().tabs == []


def test_a_damaged_file_never_stops_startup(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{ this is not json")
    assert SessionStore(path).load().tabs == []


def test_an_unknown_version_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": 99, "tabs": [{"title": "x", "text": "y"}]}))
    assert SessionStore(path).load().tabs == []


def test_malformed_tabs_are_dropped_individually(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "current": 0,
                "tabs": [
                    {"title": "good", "text": "select 1"},
                    "not a dict",
                    {"title": "no content"},
                    {"title": "bad path", "path": 42},
                    {"title": "also good", "path": "/tmp/x.sql"},
                ],
            }
        )
    )
    assert [t.title for t in SessionStore(path).load().tabs] == ["good", "also good"]


def test_out_of_range_current_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": 1, "current": 7, "tabs": [{"title": "a", "text": "x"}]}))
    assert SessionStore(path).load().current == 0


def test_oversized_buffers_are_not_carried(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionState(
            tabs=[
                TabState(title="huge", text="x" * (MAX_BUFFER_CHARS + 1)),
                TabState(title="file", path="/tmp/q.sql", text="x" * (MAX_BUFFER_CHARS + 1)),
            ]
        )
    )
    loaded = store.load()
    # The untitled tab had nothing left to restore and is dropped; the
    # file-backed one survives and re-reads from disk.
    assert [t.title for t in loaded.tabs] == ["file"]
    assert loaded.tabs[0].text is None


def test_writes_are_atomic(tmp_path: Path) -> None:
    """A failed write must not destroy the previous session."""
    path = tmp_path / "session.json"
    store = SessionStore(path)
    store.save(SessionState(tabs=[TabState(title="first", text="select 1")]))

    original = path.read_text()
    store.path = tmp_path / "missing-dir" / "deep" / "session.json"
    store.path.parent.mkdir(parents=True)
    store.path.parent.chmod(0o500)  # read-only: the write cannot succeed
    try:
        assert store.save(SessionState(tabs=[TabState(title="second", text="x")])) is False
    finally:
        store.path.parent.chmod(0o700)
    assert path.read_text() == original


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    for i in range(3):
        store.save(SessionState(tabs=[TabState(title=f"t{i}", text="x")]))
    assert [p.name for p in tmp_path.iterdir()] == ["session.json"]


def test_clear_removes_the_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionState(tabs=[TabState(title="a", text="x")]))
    store.clear()
    assert not (tmp_path / "session.json").exists()
    store.clear()  # idempotent


# -- permissions ------------------------------------------------------------


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_a_written_session_is_private(tmp_path: Path) -> None:
    """mkstemp creates at 0600 and os.replace keeps that mode."""
    path = tmp_path / "session.json"
    store = SessionStore(path)
    store.save(SessionState(tabs=[TabState(title="a", text="select 1")]))
    assert mode_of(path) == 0o600


def test_a_loose_file_from_an_earlier_version_is_tightened(tmp_path: Path) -> None:
    """Restoring clears the autosave flag, so an untouched run never saves and
    would otherwise leave the old mode in place."""
    path = tmp_path / "session.json"
    path.write_text('{"version": 1, "current": 0, "tabs": []}')
    path.chmod(0o644)

    SessionStore(path)
    assert mode_of(path) == 0o600


def test_the_owner_s_own_bits_are_left_alone(tmp_path: Path) -> None:
    """secure() only ever takes access away."""
    path = tmp_path / "session.json"
    path.write_text("{}")
    path.chmod(0o444)

    SessionStore(path)
    assert mode_of(path) == 0o400


def test_opening_a_missing_session_is_harmless(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "nothing-here.json")
    assert store.load().tabs == []
