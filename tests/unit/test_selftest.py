"""The packaging self-check (spec M0)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from snowdesk import selftest


def test_all_checks_pass_in_a_working_environment(qapp) -> None:
    """If this fails from source, the environment is broken, not the build."""
    assert selftest.run_selftest() == 0


def test_every_check_reports_rather_than_raising() -> None:
    check = selftest._run("boom", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert not check.ok
    assert "RuntimeError: nope" in check.detail


def test_a_failing_check_fails_the_run(monkeypatch: pytest.MonkeyPatch, qapp) -> None:
    def broken() -> str:
        raise ImportError("No module named 'snowflake.connector.snow_logging'")

    monkeypatch.setattr(selftest, "CHECKS", [("Arrow result reader", broken)])
    assert selftest.run_selftest() == 1


def test_the_report_names_each_check(capsys, qapp) -> None:
    selftest.run_selftest()
    report = capsys.readouterr().err
    for name, _fn in selftest.CHECKS:
        assert name in report
    assert "checks passed" in report


def test_arrow_reader_is_importable() -> None:
    """The extension that PyInstaller could not find on its own."""
    assert "loaded" in selftest._check_arrow()


# -- the connection check's passphrase prompt -------------------------------

MISSING = TypeError("Password was not given but private key is encrypted")
WRONG = ValueError("Incorrect password, could not decrypt key")


class _FakeSession:
    def __init__(self, version: str = "9.1.0") -> None:
        self.closed = False
        self._version = version

    def read_context(self):
        from snowdesk.model import SessionContext

        return SessionContext(role="ANALYST", warehouse="COMPUTE_WH")

    @property
    def connection(self):
        version = self._version

        class Cur:
            description: ClassVar = [("CURRENT_VERSION()", 2, None, None, None, None, True)]

            def execute(self, _sql: str) -> None:
                pass

            def fetchone(self):
                return (version,)

            def close(self) -> None:
                pass

        return type("Conn", (), {"cursor": staticmethod(Cur)})()

    def close(self) -> None:
        self.closed = True


def _stub_open(monkeypatch, unlock_with: str | None) -> list[str | None]:
    """Record each passphrase tried; succeed only on ``unlock_with``."""
    tried: list[str | None] = []

    def fake_open(_name: str, passphrase: str | None):
        tried.append(passphrase)
        if passphrase is None:
            raise MISSING
        if passphrase != unlock_with:
            raise WRONG
        return _FakeSession()

    monkeypatch.setattr(selftest, "_open_session", fake_open)
    return tried


def test_encrypted_key_is_unlocked_from_the_terminal(monkeypatch) -> None:
    tried = _stub_open(monkeypatch, "right")
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: "right")
    check = selftest._check_snowflake("dev")
    assert check.ok
    assert "Snowflake 9.1.0" in check.detail
    assert tried == [None, "right"]


def test_a_wrong_passphrase_is_retried(monkeypatch) -> None:
    tried = _stub_open(monkeypatch, "right")
    replies = iter(["wrong", "right"])
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: next(replies))
    assert selftest._check_snowflake("dev").ok
    assert tried == [None, "wrong", "right"]


def test_retries_are_bounded(monkeypatch) -> None:
    tried = _stub_open(monkeypatch, "never-matches")
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: "wrong")
    check = selftest._check_snowflake("dev")
    assert not check.ok
    assert len(tried) == selftest._PASSPHRASE_ATTEMPTS


def test_no_terminal_reports_instead_of_hanging(monkeypatch) -> None:
    """A build launched from Finder has no tty; the GUI prompts instead."""
    _stub_open(monkeypatch, "right")
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: None)
    check = selftest._check_snowflake("dev")
    assert not check.ok
    assert "private key is encrypted" in check.detail


def test_other_connect_failures_are_not_retried(monkeypatch) -> None:
    calls: list[str] = []

    def fake_open(_name: str, _passphrase: str | None):
        calls.append(_name)
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(selftest, "_open_session", fake_open)
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: "x")
    check = selftest._check_snowflake("dev")
    assert not check.ok
    assert "network unreachable" in check.detail
    assert len(calls) == 1


def test_the_prompt_is_never_echoed_into_the_report(monkeypatch, capsys, qapp) -> None:
    _stub_open(monkeypatch, "right")
    monkeypatch.setattr(selftest, "_prompt_passphrase", lambda _n, retry: "right")
    monkeypatch.setattr(selftest, "CHECKS", [])
    selftest.run_selftest("dev")
    assert "right" not in capsys.readouterr().err
