"""What SnowDesk writes to disk stays with its owner.

The history database holds the text of every statement run, which is where
credentials appear in the open (``CREATE USER ... PASSWORD=``), and the log
holds what the connector was doing.  Neither should be readable by another
account on the machine, the way the connector already requires of
``connections.toml``.
"""

from __future__ import annotations

import stat
from pathlib import Path

from snowdesk import config
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionState, SessionStore, TabState

from .test_history import outcome


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_removes_group_and_world_access(tmp_path: Path) -> None:
    path = tmp_path / "readable.db"
    path.write_text("x")
    path.chmod(0o644)
    config.secure(path)
    assert mode_of(path) == 0o600


def test_secure_leaves_the_owners_own_bits_alone(tmp_path: Path) -> None:
    """Tightening must never hand out access the owner had taken away."""
    path = tmp_path / "read-only.sql"
    path.write_text("select 1")
    path.chmod(0o444)
    config.secure(path)
    assert mode_of(path) == 0o400  # still read-only, no longer world-readable


def test_secure_is_quiet_about_a_file_that_is_gone(tmp_path: Path) -> None:
    config.secure(tmp_path / "never-existed")  # must not raise


def test_private_dir_creates_and_tightens(tmp_path: Path) -> None:
    fresh = tmp_path / "support"
    assert mode_of(config.private_dir(fresh)) == 0o700

    fresh.chmod(0o755)  # as an earlier version would have left it
    assert mode_of(config.private_dir(fresh)) == 0o700


def test_history_database_is_not_world_readable(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "support" / "history.db")
    try:
        store.record(outcome("create user bob password='hunter2'"), "dev")
    finally:
        store.close()
    assert mode_of(tmp_path / "support" / "history.db") & 0o077 == 0
    assert mode_of(tmp_path / "support") & 0o077 == 0


def test_session_file_is_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "support" / "session.json"
    assert SessionStore(path).save(SessionState(tabs=[TabState(text="select 1")]))
    assert mode_of(path) & 0o077 == 0
