"""Packaging self-check (spec M0).

``snowdesk --selftest`` verifies that a build can actually load everything
SnowDesk needs at runtime: Qt, the connector and its compiled Arrow reader, the
crypto stack used for key-pair auth, and the CA bundle.  Copying a file into a
bundle is not the same as being able to import it, and PyInstaller failures
show up exactly here.

With ``--connection NAME`` it goes on to open a session and run
``SELECT CURRENT_VERSION()``, which is the M0 exit criterion in full.
"""

from __future__ import annotations

import logging
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _run(name: str, fn: Callable[[], str]) -> Check:
    try:
        return Check(name, True, fn())
    except BaseException as exc:
        log.debug("Self-check %s failed", name, exc_info=True)
        return Check(name, False, f"{type(exc).__name__}: {exc}")


def _check_qt() -> str:
    import PySide6
    from PySide6.QtWidgets import QApplication, QMainWindow

    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setWindowTitle("SnowDesk self-test")
    window.resize(320, 200)
    window.show()
    app.processEvents()
    visible = window.isVisible()
    window.close()
    if not visible:
        raise RuntimeError("window did not become visible")
    return f"PySide6 {PySide6.__version__}, window opened"


def _check_connector() -> str:
    import snowflake.connector

    return f"snowflake-connector-python {snowflake.connector.__version__}"


def _check_arrow() -> str:
    """The compiled result reader — the piece most likely to be missed."""
    from snowflake.connector.nanoarrow_arrow_iterator import PyArrowRowIterator

    return f"{PyArrowRowIterator.__module__} loaded"


def _check_crypto() -> str:
    """Round-trip an encrypted key, as key-pair auth does at connect time."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"self-test"),
    )
    serialization.load_pem_private_key(pem, password=b"self-test")
    return "RSA key encrypt/decrypt round-trip"


def _check_tls() -> str:
    import ssl

    context = ssl.create_default_context()
    stats = context.cert_store_stats()
    if not stats.get("x509_ca"):
        raise RuntimeError("no CA certificates available")
    return f"{stats['x509_ca']} CA certificates"


def _check_config() -> str:
    from snowdesk import config

    names = [c.name for c in config.list_connections()]
    if not names:
        return f"no connections in {config.config_dir()}"
    return f"{len(names)} connection(s): {', '.join(names)}"


def _check_sqlite() -> str:
    import sqlite3
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        from snowdesk.storage.history import HistoryStore

        store = HistoryStore(Path(tmp) / "h.db")
        store.recent()
        store.close()
    return f"sqlite3 {sqlite3.sqlite_version}"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("Qt", _check_qt),
    ("Snowflake connector", _check_connector),
    ("Arrow result reader", _check_arrow),
    ("Crypto (key-pair auth)", _check_crypto),
    ("TLS trust store", _check_tls),
    ("SQLite history", _check_sqlite),
    ("Connection config", _check_config),
]


def _prompt_passphrase(name: str, *, retry: bool) -> str | None:
    """Ask for an encrypted key's passphrase on the terminal.

    ``getpass`` reads from the controlling terminal without echoing.  A build
    started from Finder has no terminal, so this reports rather than hanging;
    the GUI asks in a dialog instead.
    """
    import getpass

    headline = (
        "Incorrect passphrase, try again" if retry else f"The private key for {name!r} is encrypted"
    )
    try:
        return getpass.getpass(f"{headline}. Passphrase: ") or None
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        log.debug("Could not prompt for a passphrase", exc_info=True)
        return None


def _open_session(name: str, passphrase: str | None):
    from snowdesk.db.session import ConnectParams, SnowflakeSession

    session = SnowflakeSession()
    session.connect(ConnectParams(name=name, private_key_passphrase=passphrase))
    return session


#: Wrong passphrases are cheap to retype and expensive to get wrong once.
_PASSPHRASE_ATTEMPTS = 3


def _connect(name: str):
    """Connect, asking for the key passphrase if the key turns out encrypted."""
    from snowdesk.db.errors import (
        is_bad_private_key_passphrase,
        needs_private_key_passphrase,
    )

    passphrase: str | None = None
    for _attempt in range(_PASSPHRASE_ATTEMPTS):
        try:
            return _open_session(name, passphrase)
        except BaseException as exc:
            rejected = is_bad_private_key_passphrase(exc)
            if not (rejected or needs_private_key_passphrase(exc)):
                raise
            passphrase = _prompt_passphrase(name, retry=rejected)
            if passphrase is None:
                raise
    raise RuntimeError("Too many incorrect passphrase attempts")


def _check_snowflake(name: str) -> Check:
    """Open a real session and read CURRENT_VERSION() (M0 exit criterion)."""

    def run() -> str:
        session = _connect(name)
        try:
            ctx = session.read_context()
            cur = session.connection.cursor()
            try:
                cur.execute("SELECT CURRENT_VERSION()")
                row = cur.fetchone()
            finally:
                cur.close()
        finally:
            session.close()
        version = row[0] if row else "?"
        return f"Snowflake {version} — {ctx}"

    return _run(f"Connect to {name!r}", run)


def run_selftest(connection: str | None = None) -> int:
    """Run every check, print a report, and return a process exit code."""
    lines = [
        "SnowDesk self-test",
        f"  python   {sys.version.split()[0]} ({platform.machine()})",
        f"  frozen   {getattr(sys, 'frozen', False)}",
        f"  bundle   {getattr(sys, '_MEIPASS', '-')}",
        "",
    ]
    checks = [_run(name, fn) for name, fn in CHECKS]
    if connection:
        checks.append(_check_snowflake(connection))

    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"  [{mark}] {check.name}: {check.detail}")

    failed = [c for c in checks if not c.ok]
    lines.extend(["", f"{len(checks) - len(failed)}/{len(checks)} checks passed"])
    report = "\n".join(lines)

    # A windowed bundle may have no usable stdout, so the log file is the
    # dependable channel; print as well for the common terminal case.
    print(report, file=sys.stderr)
    for line in report.splitlines():
        log.info("%s", line)
    return 1 if failed else 0
