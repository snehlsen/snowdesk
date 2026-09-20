"""Editor session persistence (E2, spec 7.8).

Tab contents are autosaved to the application support folder so that quitting
and relaunching restores what was being worked on, including tabs that were
never saved to a file.

A tab backed by a file that has not been edited stores only its path and is
re-read from disk on restore; only unsaved work is copied into the session
file.  That keeps the file small, keeps a single source of truth for saved
files, and means an externally edited file comes back with its new contents.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowdesk import config

log = logging.getLogger(__name__)

SESSION_VERSION = 1

#: Buffers larger than this are not carried across a restart.  A tab holding
#: megabytes of generated SQL is not worth rewriting to disk every few seconds;
#: if it is backed by a file it still restores from the file.
MAX_BUFFER_CHARS = 1_000_000


@dataclass(slots=True)
class TabState:
    """One editor tab, as persisted."""

    title: str = "Untitled"
    path: str | None = None
    #: Unsaved text.  ``None`` means "re-read from ``path``".
    text: str | None = None
    cursor: int = 0

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"title": self.title, "cursor": self.cursor}
        if self.path:
            data["path"] = self.path
        if self.text is not None:
            data["text"] = self.text
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TabState | None:
        if not isinstance(data, dict):
            return None
        title = data.get("title")
        path = data.get("path")
        text = data.get("text")
        if not isinstance(title, str) or not title:
            title = "Untitled"
        if path is not None and not isinstance(path, str):
            return None
        if text is not None and not isinstance(text, str):
            return None
        if path is None and text is None:
            return None  # nothing to restore
        cursor = data.get("cursor")
        return cls(
            title=title,
            path=path,
            text=text,
            cursor=cursor if isinstance(cursor, int) and cursor >= 0 else 0,
        )


@dataclass(slots=True)
class SessionState:
    tabs: list[TabState] = field(default_factory=list)
    current: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SESSION_VERSION,
            "current": self.current,
            "tabs": [t.to_json() for t in self.tabs],
        }

    @classmethod
    def from_json(cls, data: Any) -> SessionState:
        if not isinstance(data, dict) or data.get("version") != SESSION_VERSION:
            return cls()
        raw_tabs = data.get("tabs")
        if not isinstance(raw_tabs, list):
            return cls()
        tabs = [t for t in (TabState.from_json(item) for item in raw_tabs) if t is not None]
        current = data.get("current")
        if not isinstance(current, int) or not 0 <= current < len(tabs):
            current = 0
        return cls(tabs=tabs, current=current)


class SessionStore:
    """Reads and atomically writes the editor session file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Writing goes through mkstemp, which creates at 0600, so a file this
        # version wrote is already private.  One left loose by an earlier
        # version would never be tightened otherwise: restoring clears the
        # autosave flag, so a run where nothing is edited never saves, and the
        # file keeps whatever mode it had.  Tightened here instead, as the
        # history database is.
        if self.path.exists():
            config.secure(self.path)

    def load(self) -> SessionState:
        """Read the session, returning an empty one if it is absent or damaged.

        A corrupt session file must never stop the app from starting, so any
        read problem is logged and treated as "no session".
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SessionState()
        except OSError:
            log.warning("Could not read session file %s", self.path, exc_info=True)
            return SessionState()
        try:
            return SessionState.from_json(json.loads(raw))
        except ValueError:
            log.warning("Ignoring damaged session file %s", self.path)
            return SessionState()

    def save(self, state: SessionState) -> bool:
        """Write the session atomically. Returns whether it was written."""
        payload = state.to_json()
        for tab in payload["tabs"]:
            text = tab.get("text")
            if isinstance(text, str) and len(text) > MAX_BUFFER_CHARS:
                # Too big to carry; a file-backed tab still restores from disk.
                del tab["text"]
        try:
            config.private_dir(self.path.parent)
            # Write beside the target and rename, so a crash or a full disk
            # never leaves a half-written session behind.  mkstemp also opens
            # at 0600, which is what the restored file should keep: unsaved
            # editor buffers are the user's working notes.
            fd, tmp_name = tempfile.mkstemp(
                dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError:
            log.warning("Could not write session file %s", self.path, exc_info=True)
            return False
        return True

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
