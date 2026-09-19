"""Encrypted private keys: prompting instead of failing (C3)."""

from __future__ import annotations

import pytest

from snowdesk.db.errors import is_bad_private_key_passphrase, needs_private_key_passphrase
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from tests.fakes import FakeConnection, FakeProgrammingError

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


def _worker(exc: BaseException | None, qapp) -> tuple[SnowflakeWorker, list[ConnectParams]]:
    attempts: list[ConnectParams] = []

    def connect_fn(params: ConnectParams) -> FakeConnection:
        attempts.append(params)
        if exc is not None and params.private_key_passphrase is None:
            raise exc
        if exc is not None and params.private_key_passphrase != "right":
            raise WRONG
        return FakeConnection()

    return SnowflakeWorker(session=SnowflakeSession(connect_fn=connect_fn)), attempts


def test_missing_passphrase_prompts_rather_than_erroring(qapp) -> None:
    worker, _attempts = _worker(MISSING, qapp)
    prompts = collect(worker.passphrase_required)
    failures = collect(worker.connect_failed)
    states = collect(worker.state_changed)

    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))

    assert prompts == [("dev", False)]
    assert failures == []  # not surfaced as a dead-end error
    assert states[-1][0] == "disconnected"


def test_wrong_passphrase_prompts_again_as_rejected(qapp) -> None:
    worker, _attempts = _worker(MISSING, qapp)
    prompts = collect(worker.passphrase_required)

    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev", private_key_passphrase="nope")))

    assert prompts == [("dev", False), ("dev", True)]


def test_correct_passphrase_connects(qapp) -> None:
    worker, _attempts = _worker(MISSING, qapp)
    connected = collect(worker.connected)

    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev", private_key_passphrase="right")))

    assert [c[0] for c in connected] == ["dev"]
    assert worker.session.is_connected


def test_passphrase_is_remembered_for_the_run(qapp) -> None:
    """Disconnect then Connect must not ask again."""
    worker, attempts = _worker(MISSING, qapp)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev", private_key_passphrase="right")))
    prompts = collect(worker.passphrase_required)

    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))

    assert prompts == []
    assert attempts[-1].private_key_passphrase == "right"


def test_a_rejected_passphrase_is_forgotten(qapp) -> None:
    worker, _attempts = _worker(MISSING, qapp)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev", private_key_passphrase="right")))
    # The key changed under us; the remembered passphrase no longer works.
    worker.session._connect_fn = lambda _p: (_ for _ in ()).throw(WRONG)
    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))
    assert worker._passphrases == {}


def test_other_connect_errors_still_surface_as_errors(qapp) -> None:
    worker, _attempts = _worker(FakeProgrammingError("Incorrect username", errno=390100), qapp)
    prompts = collect(worker.passphrase_required)
    failures = collect(worker.connect_failed)
    states = collect(worker.state_changed)

    worker._dispatch(ConnectJob(params=ConnectParams(name="dev")))

    assert prompts == []
    assert failures[0].errno == 390100
    assert states[-1][0] == "error"
