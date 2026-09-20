"""Log noise and the connector's platform fingerprinting."""

from __future__ import annotations

import logging
import os

import pytest

from snowdesk import app
from snowdesk.db.session import (
    DISABLE_PLATFORM_DETECTION_VAR,
    ConnectParams,
    _default_connect,
    disable_platform_detection,
)

# -- no telemetry (spec 5) --------------------------------------------------


def test_platform_detection_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISABLE_PLATFORM_DETECTION_VAR, raising=False)
    disable_platform_detection()
    assert os.environ[DISABLE_PLATFORM_DETECTION_VAR] == "true"


def test_an_explicit_setting_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone who deliberately turns it back on gets what they asked for."""
    monkeypatch.setenv(DISABLE_PLATFORM_DETECTION_VAR, "false")
    disable_platform_detection()
    assert os.environ[DISABLE_PLATFORM_DETECTION_VAR] == "false"


def test_connecting_opts_out_before_it_reaches_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result is cached per process, so it has to be set before the first
    connect, not merely at some point during startup."""
    import snowflake.connector

    monkeypatch.delenv(DISABLE_PLATFORM_DETECTION_VAR, raising=False)
    seen: list[str | None] = []

    def fake_connect(**kwargs: object) -> object:
        seen.append(os.environ.get(DISABLE_PLATFORM_DETECTION_VAR))
        return object()

    monkeypatch.setattr(snowflake.connector, "connect", fake_connect)
    _default_connect(ConnectParams(name="dev"))
    assert seen == ["true"]


def test_the_connection_carries_no_extra_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the parameters SnowDesk means to send are sent."""
    import snowflake.connector

    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(snowflake.connector, "connect", fake_connect)
    _default_connect(ConnectParams(name="dev"))
    assert set(captured) == {
        "connection_name",
        "client_session_keep_alive",
        "client_store_temporary_credential",
        "session_parameters",
    }


# -- log noise --------------------------------------------------------------


#: Loggers these tests measure.  pytest's logging plugin sets levels on
#: individual loggers, so they are reset to NOTSET first: otherwise the test
#: measures the test runner rather than setup_logging.
_WATCHED = (
    "snowdesk",
    "snowdesk.db.worker",
    "botocore",
    "botocore.credentials",
    "snowflake.connector",
    "urllib3",
)


@pytest.fixture
def fresh_logging():
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in _WATCHED}
    saved_handlers = root.handlers[:]
    saved_root_level = root.level

    root.handlers = []
    for name in _WATCHED:
        logging.getLogger(name).setLevel(logging.NOTSET)
    yield

    root.handlers = saved_handlers
    root.setLevel(saved_root_level)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def configure(**kwargs: object) -> None:
    """Run setup_logging for real.

    It returns early when the root logger already has handlers, and pytest's
    logging plugin attaches its own around each test, so they are cleared here
    rather than in the fixture -- the plugin re-attaches after fixture setup.
    """
    logging.getLogger().handlers = []
    app.setup_logging(**kwargs)  # type: ignore[arg-type]


def test_third_party_chatter_is_kept_out_of_the_log(fresh_logging) -> None:
    """An ordinary connect used to write botocore and urllib3 lines at INFO."""
    configure()
    assert logging.getLogger("botocore.credentials").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("snowflake.connector").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("urllib3").getEffectiveLevel() == logging.WARNING


def test_snowdesk_still_logs_at_info(fresh_logging) -> None:
    configure()
    assert logging.getLogger("snowdesk.db.worker").getEffectiveLevel() == logging.INFO


def test_configuring_twice_does_not_duplicate_handlers(fresh_logging) -> None:
    configure()
    before = len(logging.getLogger().handlers)
    app.setup_logging()
    assert len(logging.getLogger().handlers) == before


def test_verbose_opens_everything_up(fresh_logging) -> None:
    configure(verbose=True)
    assert logging.getLogger("botocore.credentials").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("snowdesk.db.worker").getEffectiveLevel() == logging.DEBUG
