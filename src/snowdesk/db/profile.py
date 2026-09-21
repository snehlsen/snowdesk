"""Query profile lookup: ``GET_QUERY_OPERATOR_STATS`` for one query id.

Every statement SnowDesk runs goes through ``execute_async``, and the connector
collects the result with ``select * from table(result_scan('<qid>'))`` on the
same session.  That wrapper is then the session's *last* query, so
``LAST_QUERY_ID()`` and the newest row of ``QUERY_HISTORY`` both point at it
rather than at the statement that was run -- and its profile is a single
RESULT_SCAN node over a cached result, which is what makes a profile taken that
way look empty.

The only dependable handle on the real query is the id SnowDesk kept when it
ran it (``StatementOutcome.query_id``), so everything here takes that id
explicitly and nothing here ever asks Snowflake what ran last.
"""

from __future__ import annotations

import re

from snowdesk.db.identifiers import quote_literal

#: Snowflake query ids are UUIDs.  Checked loosely -- hex and dashes, bounded
#: length -- rather than against the exact UUID shape: the point is to reject a
#: stray value here, with something readable, instead of at the server, and a
#: stricter pattern would break the feature outright if the format ever moved.
#: Injection is not what this guards; ``quote_literal`` does that.
_QUERY_ID = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{6,62}[0-9a-fA-F]$")


def is_query_id(value: str | None) -> bool:
    return bool(value and _QUERY_ID.match(value.strip()))


def short_id(query_id: str) -> str:
    """The leading segment, for a tab label that has to fit."""
    return query_id.strip().split("-", 1)[0]


def profile_sql(query_id: str) -> str:
    """Operator statistics for ``query_id``, in execution-plan order.

    Ordered because the grid is the whole reading experience here: unordered
    operator rows are a plan shuffled into no particular shape.
    """
    return (
        "SELECT * FROM TABLE(GET_QUERY_OPERATOR_STATS("
        f"{quote_literal(query_id.strip())}))"
        " ORDER BY STEP_ID, OPERATOR_ID"
    )


def empty_hint(query_id: str) -> str:
    """Why a query that definitely ran can still have no operator statistics."""
    return (
        f"No operator statistics for {query_id}.\n"
        "Snowflake keeps a profile only for queries that did work on a "
        "warehouse, so a result-cache hit, a metadata-only query (many "
        "COUNT(*) and MIN/MAX answers), a SHOW or DESCRIBE, DDL, and a "
        "statement that failed before execution all have none. Statistics are "
        "also dropped after 14 days."
    )
