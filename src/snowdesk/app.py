"""QApplication setup, logging, and worker-thread lifecycle."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from PySide6.QtCore import QThread
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from snowdesk import __version__, config
from snowdesk.controllers.browser import BrowserController
from snowdesk.controllers.query import QueryController
from snowdesk.db.worker import SnowflakeWorker
from snowdesk.storage.history import HistoryStore
from snowdesk.storage.session import SessionStore
from snowdesk.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    """Log to ``~/Library/Logs/SnowDesk/snowdesk.log`` plus stderr (spec 9)."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        path = config.log_dir() / "snowdesk.log"
        file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # a missing log directory must never stop the app from starting

    # A windowed bundle launched from Finder can have no stderr at all; a
    # StreamHandler on None fails on every record, so the file log stands alone.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def is_dark(app: QApplication) -> bool:
    palette = app.palette()
    window = palette.color(QPalette.ColorRole.Window)
    return window.lightness() < 128


class Application:
    """Owns the worker thread and tears it down cleanly on quit."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self.qt = QApplication(argv if argv is not None else sys.argv)
        self.qt.setApplicationName("SnowDesk")
        self.qt.setOrganizationName("SnowDesk")

        self.history = HistoryStore(config.history_db_path())
        self.session = SessionStore(config.session_path())
        self.worker = SnowflakeWorker()
        self.thread = QThread()
        self.thread.setObjectName("snowdesk-worker")
        self.worker.moveToThread(self.thread)
        # The loop pulls from its own queue, so this thread deliberately does
        # not run a Qt event loop; signals are still delivered to the UI thread.
        self.thread.started.connect(self.worker.run_loop)

        self.query = QueryController(self.worker, history=self.history)
        self.browser = BrowserController(self.worker)
        self.window = MainWindow(
            worker=self.worker,
            query=self.query,
            browser=self.browser,
            history=self.history,
            session=self.session,
            dark=is_dark(self.qt),
        )
        self.qt.aboutToQuit.connect(self.shutdown)

    def run(self) -> int:
        self.thread.start()
        self.window.show()
        return int(self.qt.exec())

    def shutdown(self) -> None:
        log.info("Shutting down")
        self.window.editors.save_session()
        # Break any polling loop first so the worker reaches the shutdown job.
        self.worker.cancel_running()
        self.worker.shutdown()
        self.thread.quit()
        if not self.thread.wait(5000):
            log.warning("Worker thread did not stop in time")
        self.history.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv[1:]
    parser = argparse.ArgumentParser(prog="snowdesk", description=__doc__)
    parser.add_argument("--version", action="version", version=f"snowdesk {__version__}")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check that this build can load Qt, the connector and the crypto stack",
    )
    parser.add_argument(
        "--connection",
        metavar="NAME",
        help="with --selftest, also connect and read CURRENT_VERSION()",
    )
    parsed, _unknown = parser.parse_known_args(args)

    setup_logging()
    if parsed.selftest:
        from snowdesk.selftest import run_selftest

        return run_selftest(parsed.connection)
    return Application(argv).run()
