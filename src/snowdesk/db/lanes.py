"""Where the worker's work runs: its execution lanes (spec 6.2).

The worker runs Snowflake calls in four lanes:

* **session**: one thread that owns the connection; jobs run in the order
  they were submitted, and only this lane may close the session.
* **export**: CSV exports, so a million rows does not block every query.
* **transfer**: stage plans and transfers, so a PUT does not either.
* **cancel**: ``SYSTEM$CANCEL_QUERY``, which must not wait behind the
  statement it is cancelling.

:meth:`Lanes.threaded` is the production adapter.  :meth:`Lanes.synchronous`
runs the work on the calling thread instead, so tests can assert right
after a call without draining queues or swapping pools.
"""

from __future__ import annotations

import logging
import queue
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

Work = Callable[[], object]


class Lane(Protocol):
    def submit(self, fn: Work) -> None:
        """Run ``fn`` in this lane, after anything submitted before it."""

    def stop(self) -> None:
        """Run nothing more.  Returns without waiting for running work."""


class SessionLane(Lane, Protocol):
    def run(self, on_error: Callable[[Exception], None]) -> None:
        """Run submitted work on the calling thread until :meth:`stop`.

        ``on_error`` hears about work that raised; the lane carries on.
        """


class QueueLane:
    """A queue drained by whichever thread calls :meth:`run`."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Work | None] = queue.Queue()

    def submit(self, fn: Work) -> None:
        self._queue.put(fn)

    def stop(self) -> None:
        self._queue.put(None)

    def run(self, on_error: Callable[[Exception], None]) -> None:
        while (fn := self._queue.get()) is not None:
            try:
                fn()
            except Exception as exc:
                on_error(exc)


class ThreadLane:
    """One side-thread, so work runs in the order it was asked for."""

    def __init__(self, name: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"snowdesk-{name}")

    def submit(self, fn: Work) -> None:
        self._pool.submit(fn)

    def stop(self) -> None:
        self._pool.shutdown(wait=False)


class InlineLane:
    """Runs work on the calling thread, as soon as it is submitted.

    Work submitted while other work is running (from a signal's slot, say)
    waits for it to finish, as it would behind a real queue.
    """

    def __init__(self) -> None:
        self._pending: deque[Work] = deque()
        self._running = False

    def submit(self, fn: Work) -> None:
        self._pending.append(fn)
        if not self._running:
            self._run_pending()

    @contextmanager
    def held(self) -> Iterator[None]:
        """Keep what is submitted in the block waiting, then run it.

        For tests that look at work which has been asked for but not done.
        """
        self._running = True
        try:
            yield
        finally:
            self._running = False
        self._run_pending()

    def _run_pending(self) -> None:
        self._running = True
        try:
            while self._pending:
                self._pending.popleft()()
        finally:
            self._running = False

    def stop(self) -> None:
        self._pending.clear()

    def run(self, on_error: Callable[[Exception], None]) -> None:
        """Nothing waits to be run: :meth:`submit` already ran it."""


@dataclass(frozen=True, slots=True)
class Lanes:
    session: SessionLane
    export: Lane
    transfer: Lane
    cancel: Lane

    @classmethod
    def threaded(cls) -> Lanes:
        return cls(
            session=QueueLane(),
            export=ThreadLane("export"),
            transfer=ThreadLane("transfer"),
            cancel=ThreadLane("cancel"),
        )

    @classmethod
    def synchronous(cls) -> Lanes:
        """Every lane on the calling thread, except cancel.

        A cancel has to arrive while a statement is still running, which
        only a real second thread can do.
        """
        return cls(
            session=InlineLane(),
            export=InlineLane(),
            transfer=InlineLane(),
            cancel=ThreadLane("cancel"),
        )
