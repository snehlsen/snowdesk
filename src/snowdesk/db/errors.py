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
