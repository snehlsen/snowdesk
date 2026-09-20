"""Query history in SQLite (H1, spec 7.8).

Stored locally at ``~/Library/Application Support/SnowDesk/history.db`` and
clearable by the user.

Nothing leaves the machine, but the file is still the most sensitive thing
SnowDesk writes: it holds the full text of every statement run, and statement
text is where credentials turn up in the open -- ``CREATE USER ... PASSWORD=``,
``CREATE STAGE ... CREDENTIALS=(...)`` -- alongside whatever a ``WHERE`` clause
had to name.  So it is created private to the owner and kept that way, rather
than at whatever the umask happens to allow.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from snowdesk import config
from snowdesk.model import RunStatus, StatementOutcome

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    connection  TEXT    NOT NULL DEFAULT '',
    sql         TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    duration_s  REAL    NOT NULL DEFAULT 0,
    row_count   INTEGER,
    query_id    TEXT,
    message     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS history_ts ON history (ts DESC);
"""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: int
    ts: float
    connection: str
    sql: str
    status: str
    duration_s: float
    row_count: int | None
    query_id: str | None
    message: str

    @property
    def first_line(self) -> str:
        line = " ".join(self.sql.split())
        return line if len(line) <= 120 else line[:117] + "…"


class HistoryStore:
    """Thread-safe SQLite store. Writes are small and local, so they are safe
    to make from either the UI thread or the worker."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent != Path("."):
            config.private_dir(self.path.parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # sqlite3 creates the file at the umask, so it is tightened as soon as
        # it exists -- before the first statement is written into it.
        config.secure(self.path)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def record(self, outcome: StatementOutcome, connection: str) -> int:
        """Persist one executed statement. Skipped statements are not recorded."""
        if outcome.status is RunStatus.SKIPPED:
            return -1
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history (ts, connection, sql, status, duration_s, row_count,"
                " query_id, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    connection,
                    outcome.statement.sql,
                    outcome.status.value,
                    outcome.duration_s,
                    outcome.row_count,
                    outcome.query_id,
                    outcome.message,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or -1)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    def recent(self, limit: int = 200) -> list[HistoryEntry]:
        return self._select("SELECT * FROM history ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def search(self, term: str, limit: int = 200) -> list[HistoryEntry]:
        """Substring search over the statement text (H2)."""
        if not term.strip():
            return self.recent(limit)
        escaped = term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._select(
            "SELECT * FROM history WHERE sql LIKE ? ESCAPE '\\' ORDER BY ts DESC, id DESC LIMIT ?",
            (f"%{escaped}%", limit),
        )

    def _select(self, sql: str, args: Iterable[object]) -> list[HistoryEntry]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [
            HistoryEntry(
                id=row["id"],
                ts=row["ts"],
                connection=row["connection"],
                sql=row["sql"],
                status=row["status"],
                duration_s=row["duration_s"],
                row_count=row["row_count"],
                query_id=row["query_id"],
                message=row["message"],
            )
            for row in rows
        ]
