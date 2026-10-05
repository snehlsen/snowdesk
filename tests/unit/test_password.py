"""Password and MFA connections with no password configured: prompting instead of failing."""

from __future__ import annotations

import pytest

from snowdesk.db.errors import (
    is_mfa_refused,
    is_rejected_password,
    is_wrong_password,
    needs_password,
)
from snowdesk.db.lanes import Lanes
from snowdesk.db.session import ConnectParams, SnowflakeSession
from snowdesk.db.worker import ConnectJob, SnowflakeWorker
from snowdesk.model import ConnectFailure, Credentials
from tests.fakes import FakeConnection, FakeProgrammingError

# What the connector and Snowflake raise, shaped the way the connector's
# login code wraps them: every failed sign-in carries "Failed to connect to DB"
# and SQLSTATE 08001, the same as a network that is down.
HOST = "Failed to connect to DB: example.snowflakecomputing.com:443."
EMPTY = FakeProgrammingError("Password is empty", errno=251006)
WRONG = FakeProgrammingError(
    f"{HOST} Incorrect username or password was specified.", errno=390100, sqlstate="28000"
)
BAD_PASSCODE = FakeProgrammingError(
    f"{HOST} Incorrect passcode was specified.", errno=390127, sqlstate="08001"
)
TOTP_NEEDED = FakeProgrammingError(
    f"{HOST} Failed to authenticate: MFA with TOTP is required. To authenticate, provide "
    "both your password and a current TOTP passcode.",
    errno=394508,
    sqlstate="08001",
)
#: A Duo push that was not approved comes back under the connector's network errno.
PUSH_FAILED = FakeProgrammingError(
    "Failed to connect to DB. MFA authentication failed: "
    "example.snowflakecomputing.com:443. Push was denied.",
    errno=250001,
    sqlstate="08001",
)
NETWORK_DOWN = FakeProgrammingError(
    "Failed to connect to DB: example.snowflakecomputing.com:443. Connection reset by peer",
    errno=250001,
    sqlstate="08001",
)


def collect(signal) -> list:
    received: list = []
    signal.connect(lambda *args: received.append(args if len(args) > 1 else args[0]))
    return received


def test_classifies_a_missing_password() -> None:
    assert needs_password(EMPTY)
    assert not needs_password(WRONG)
    assert not needs_password(TypeError("Password was not given but private key is encrypted"))


@pytest.mark.parametrize("exc", [WRONG, BAD_PASSCODE, TOTP_NEEDED, PUSH_FAILED])
def test_classifies_a_rejected_password_or_passcode(exc: BaseException) -> None:
    assert is_rejected_password(exc)


def test_tells_a_wrong_password_from_a_refused_mfa_step() -> None:
    assert is_wrong_password(WRONG) and not is_mfa_refused(WRONG)
    for exc in (BAD_PASSCODE, TOTP_NEEDED, PUSH_FAILED):
        assert is_mfa_refused(exc) and not is_wrong_password(exc)


def test_a_dropped_network_is_not_a_rejected_password() -> None:
    assert not is_rejected_password(NETWORK_DOWN)
    assert not is_rejected_password(
        FakeProgrammingError("Failed to connect to DB: MFA host unreachable", errno=250001)
    )
    assert not is_rejected_password(FakeProgrammingError("Object does not exist", errno=2003))


def test_password_and_passcode_are_kept_out_of_repr() -> None:
    """A params object can end up in a log line or traceback."""
    params = ConnectParams(name="dev", password="hunter2", passcode="123456")
    assert "hunter2" not in repr(params)
    assert "123456" not in repr(params)
    creds = Credentials("hunter2", "123456")
    assert "hunter2" not in repr(creds)
    assert "123456" not in repr(creds)


def test_password_and_passcode_are_passed_to_the_connector(monkeypatch) -> None:
    import snowflake.connector

    seen: list[dict] = []
    monkeypatch.setattr(snowflake.connector, "connect", lambda **kw: seen.append(kw))
    from snowdesk.db.session import _default_connect

    _default_connect(ConnectParams(name="dev", password="hunter2", passcode="123456"))
    assert seen[0]["connection_name"] == "dev"
    assert seen[0]["password"] == "hunter2"
    assert seen[0]["passcode"] == "123456"

    seen.clear()
    _default_connect(ConnectParams(name="dev"))
    # Nothing given, nothing overridden: connections.toml still has its say.
    assert "password" not in seen[0] and "passcode" not in seen[0]


def test_the_session_does_not_keep_the_password_or_passcode() -> None:
    session = SnowflakeSession(connect_fn=lambda _params: FakeConnection())
    session.connect(ConnectParams(name="dev", password="hunter2", passcode="123456"))
    assert session.connection_name == "dev"
    assert session._params is not None
    assert session._params.password is None
    assert session._params.passcode is None


@pytest.mark.parametrize(
    ("exc", "password", "kind"),
    [
        (EMPTY, None, ConnectFailure.PASSWORD_NEEDED),
        (WRONG, "typed", ConnectFailure.PASSWORD_REJECTED),
        (BAD_PASSCODE, "typed", ConnectFailure.PASSWORD_REJECTED),
        # A wrong password from connections.toml is the file's to fix.
        (WRONG, None, ConnectFailure.ERROR),
        # The file's password got through; only the passcode is missing.
        (TOTP_NEEDED, None, ConnectFailure.PASSCODE_NEEDED),
        (BAD_PASSCODE, None, ConnectFailure.PASSCODE_NEEDED),
        (PUSH_FAILED, None, ConnectFailure.PASSCODE_NEEDED),
        (NETWORK_DOWN, None, ConnectFailure.ERROR),
    ],
)
def test_the_worker_says_why_a_password_connect_failed(
    qapp, exc: BaseException, password: str | None, kind: ConnectFailure
) -> None:
    def connect_fn(_params: ConnectParams) -> FakeConnection:
        raise exc

    worker = SnowflakeWorker(
        session=SnowflakeSession(connect_fn=connect_fn), lanes=Lanes.synchronous()
    )
    failures = collect(worker.connect_failed)
    worker.submit(ConnectJob(params=ConnectParams(name="dev", password=password)))
    assert [(name, k) for name, k, _error in failures] == [("dev", kind)]
