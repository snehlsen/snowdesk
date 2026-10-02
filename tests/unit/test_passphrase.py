"""Encrypted private keys: prompting instead of failing (C3)."""

from __future__ import annotations

import pytest

from snowdesk.db.errors import is_bad_private_key_passphrase, needs_private_key_passphrase
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from snowdesk.model import ConnectFailure
from tests.fakes import FakeConnection, FakeProgrammingError, drain

# The exact exceptions cryptography raises through the connector.
MISSING = TypeError("Password was not given but private key is encrypted")
WRONG = ValueError("Incorrect password, could not decrypt key")
WRONG_OLD = ValueError("Bad decrypt. Incorrect password?")


def collect(signal) -> list:
    received: list = []
    signal.connect(lambda *args: received.append(args if len(args) > 1 else args[0]))
    return received


def test_classifies_a_missing_passphrase() -> None:
    assert needs_private_key_passphrase(MISSING)
    assert not needs_private_key_passphrase(WRONG)
    assert not needs_private_key_passphrase(FakeProgrammingError("bad password", errno=390100))


@pytest.mark.parametrize("exc", [WRONG, WRONG_OLD])
def test_classifies_a_rejected_passphrase(exc: BaseException) -> None:
    assert is_bad_private_key_passphrase(exc)
    assert not is_bad_private_key_passphrase(ValueError("something else entirely"))


def test_passphrase_is_passed_to_the_connector() -> None:
    seen: list[dict] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        # Mirrors the override dict built in db.session._default_connect.
        seen.append(
            {
                k: v
                for k, v in {
                    "role": params.role,
                    "warehouse": params.warehouse,
                    "private_key_file_pwd": params.private_key_passphrase,
                }.items()
                if v
            }
        )
        return FakeConnection()

    session = SnowflakeSession(connect_fn=connect_fn)
    session.connect(ConnectParams(name="dev", private_key_passphrase="s3cret"))
    assert seen == [{"private_key_file_pwd": "s3cret"}]


def test_passphrase_is_kept_out_of_repr() -> None:
    """A params object can end up in a log line or traceback."""
    params = ConnectParams(name="dev", private_key_passphrase="s3cret")
    assert "s3cret" not in repr(params)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (MISSING, ConnectFailure.PASSPHRASE_NEEDED),
        (WRONG, ConnectFailure.PASSPHRASE_REJECTED),
        (FakeProgrammingError("Incorrect username", errno=390100), ConnectFailure.ERROR),
    ],
)
def test_the_worker_says_why_a_connect_failed(qapp, exc: BaseException, kind) -> None:
    """An encrypted key is a question for the user, not a dead end (C3)."""

    def connect_fn(_params: ConnectParams) -> FakeConnection:
        raise exc

    worker = SnowflakeWorker(session=SnowflakeSession(connect_fn=connect_fn))
    failures = collect(worker.connect_failed)
    drain(worker, ConnectJob(params=ConnectParams(name="dev")))
    assert [(name, k) for name, k, _error in failures] == [("dev", kind)]
