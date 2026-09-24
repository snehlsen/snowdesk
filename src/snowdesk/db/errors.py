"""Mapping connector exceptions to :class:`snowdesk.model.QueryError` (Q5)."""

from __future__ import annotations

from typing import Any

from snowdesk.model import QueryError

#: Snowflake reports a cancelled statement as a plain ProgrammingError; these
#: are the markers that distinguish it from a genuine failure (spec 7.4).
_CANCEL_MARKERS = (
    "SQL execution canceled",
    "SQL execution cancelled",
    "statement is canceled",
    "query has been cancelled",
    "Query has been canceled",
)


def is_cancellation(exc: BaseException) -> bool:
    """True when ``exc`` represents a user-initiated cancel rather than an error."""
    errno = getattr(exc, "errno", None)
    if errno in (604, 606):
        return True
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in _CANCEL_MARKERS)


def to_query_error(exc: BaseException, query_id: str | None = None) -> QueryError:
    """Extract code, message, SQL state and query ID from a connector error."""
    raw: Any = getattr(exc, "raw_msg", None) or getattr(exc, "msg", None) or str(exc)
    errno = getattr(exc, "errno", None)
    sqlstate = getattr(exc, "sqlstate", None)
    sfqid = getattr(exc, "sfqid", None) or query_id
    return QueryError(
        message=str(raw).strip(),
        errno=int(errno) if isinstance(errno, int) else None,
        sqlstate=str(sqlstate) if sqlstate else None,
        query_id=str(sfqid) if sfqid else None,
    )


#: Raised by ``cryptography`` (through the connector) when a key-pair
#: connection points at an encrypted private key and no passphrase was given.
#: The wording has changed between ``cryptography`` releases, so several
#: phrasings are matched.
_MISSING_PASSPHRASE_MARKERS = (
    "password was not given but private key is encrypted",
    "private key is encrypted",
)
_BAD_PASSPHRASE_MARKERS = (
    "incorrect password",
    "bad decrypt",
    "could not deserialize key data",
)


def needs_private_key_passphrase(exc: BaseException) -> bool:
    """True when connecting failed only because the key passphrase is missing."""
    if not isinstance(exc, TypeError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _MISSING_PASSPHRASE_MARKERS)


def is_bad_private_key_passphrase(exc: BaseException) -> bool:
    """True when the supplied key passphrase did not decrypt the key."""
    if not isinstance(exc, (ValueError, TypeError)):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _BAD_PASSPHRASE_MARKERS)


#: Snowflake error codes that mean the session is gone rather than the
#: statement being wrong.
_SESSION_LOST_ERRNOS = frozenset(
    {
        390114,  # Authentication token has expired
        390104,  # Session no longer exists
        390112,  # Session expired
        390111,  # User must authenticate again
        250002,  # Connection is closed
        250003,  # Failed to send the request (network)
    }
)

#: The connector's own PUT and GET errors (``ER_INVALID_STAGE_FS`` through
#: ``ER_INTERNAL_NOT_MATCH_ENCRYPT_MATERIAL``): a missing local file, a GET
#: whose PATTERN matched nothing, a failed upload to the cloud store.  They are
#: raised as ``OperationalError``, the class that otherwise means the network
#: is gone, but the session is fine and the next statement will run.
_FILE_TRANSFER_ERRNOS = range(253001, 253009)

#: Connector exception classes raised for transport-level trouble.  Matched by
#: name so this module stays importable, and testable, without the connector.
_SESSION_LOST_TYPES = frozenset(
    {
        "OperationalError",
        "InterfaceError",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "SSLError",
        "NewConnectionError",
        "MaxRetryError",
    }
)

_SESSION_LOST_MARKERS = (
    "connection is closed",
    "session no longer exists",
    "authentication token has expired",
    "must authenticate again",
    "failed to connect",
    "connection aborted",
    "connection reset",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "max retries exceeded",
)

#: SQLSTATE class 08 is "connection exception" in the SQL standard.
_SESSION_LOST_SQLSTATE_PREFIX = "08"


def is_session_lost(exc: BaseException) -> bool:
    """True when the session or the network died, not the statement (spec 9).

    A statement that fails because SnowDesk can no longer reach Snowflake needs
    a different answer from one that fails because the SQL was wrong: the
    connection is marked dead and the user is offered a reconnect, rather than
    being shown an error beside a toolbar that still claims to be connected.
    """
    if is_cancellation(exc):
        return False
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and errno in _FILE_TRANSFER_ERRNOS:
        return False
    if isinstance(errno, int) and errno in _SESSION_LOST_ERRNOS:
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate.startswith(_SESSION_LOST_SQLSTATE_PREFIX):
        return True
    for kind in type(exc).__mro__:
        if kind.__name__ in _SESSION_LOST_TYPES:
            return True
    text = str(exc).lower()
    return any(marker in text for marker in _SESSION_LOST_MARKERS)
