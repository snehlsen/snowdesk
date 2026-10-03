"""Object browser controller: lazy expansion with per-node caching (B1)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from snowdesk.controllers.session_lifecycle import SessionLifecycle, SessionStatus
from snowdesk.db.worker import BrowseJob, SnowflakeWorker
from snowdesk.model import ObjectNode

Path = tuple[str, ...]


class BrowserController(QObject):
    """Requests one ``SHOW`` per expanded node, at most once."""

    nodes_ready = Signal(object, object)  # path, list[ObjectNode]
    failed = Signal(object, str)

    def __init__(self, worker: SnowflakeWorker, lifecycle: SessionLifecycle) -> None:
        super().__init__()
        self.worker = worker
        self._cache: dict[Path, list[ObjectNode]] = {}
        self._pending: set[Path] = set()
        worker.nodes_ready.connect(self._on_nodes)
        worker.browse_failed.connect(self._on_failed)
        lifecycle.changed.connect(self._on_session_changed)

    def cached(self, path: Path) -> list[ObjectNode] | None:
        return self._cache.get(path)

    def is_pending(self, path: Path) -> bool:
        return path in self._pending

    def load(self, path: Path = (), *, refresh: bool = False) -> None:
        """Expand ``path``; served from cache unless ``refresh`` (B5)."""
        if refresh:
            self._cache.pop(path, None)
        elif path in self._cache:
            self.nodes_ready.emit(path, self._cache[path])
            return
        if path in self._pending:
            return
        self._pending.add(path)
        self.worker.submit(BrowseJob(path=path))

    def invalidate_all(self) -> None:
        self._cache.clear()
        self._pending.clear()

    def _on_session_changed(self, status: SessionStatus) -> None:
        # On the way out of a Session rather than into the next one, so the
        # cache is empty before anyone can ask the new Session for roots.
        if not status.is_connected:
            self.invalidate_all()

    def _on_nodes(self, path: Path, nodes: list[ObjectNode]) -> None:
        self._pending.discard(path)
        self._cache[path] = nodes
        self.nodes_ready.emit(path, nodes)

    def _on_failed(self, path: Path, message: str) -> None:
        self._pending.discard(path)
        self.failed.emit(path, message)
