"""Object browser queries (B1, spec 7.7).

One ``SHOW`` per expanded node, so an account with many databases is never
enumerated eagerly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from snowdesk.db.identifiers import qualify, quote_ident, quote_literal
from snowdesk.db.session import Connection
from snowdesk.model import ObjectNode

DATABASE = "database"
SCHEMA = "schema"
TABLE = "table"
VIEW = "view"
COLUMN = "column"

#: Schemas that clutter the tree without ever being browsed.
_HIDDEN_SCHEMAS = frozenset({"INFORMATION_SCHEMA"})


def _rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    """Name the columns of a ``SHOW`` result.

    A cursor with no description produced no result set; that is an empty
    listing, not a crash whose Python traceback ends up in the sidebar.
    """
    description = getattr(cursor, "description", None)
    if not description:
        return []
    names = [str(c[0] if isinstance(c, tuple) else c.name).lower() for c in description]
    return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]


def _show(conn: Connection, sql: str) -> list[dict[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return _rows_as_dicts(cur)
    finally:
        cur.close()


def list_databases(conn: Connection) -> list[ObjectNode]:
    rows = _show(conn, "SHOW DATABASES")
    return [
        ObjectNode(
            name=str(r["name"]),
            kind=DATABASE,
            detail=str(r.get("kind") or r.get("origin") or ""),
            path=(str(r["name"]),),
        )
        for r in rows
        if r.get("name")
    ]


def list_schemas(conn: Connection, database: str) -> list[ObjectNode]:
    rows = _show(conn, f"SHOW SCHEMAS IN DATABASE {qualify(database)}")
    return [
        ObjectNode(
            name=str(r["name"]),
            kind=SCHEMA,
            path=(database, str(r["name"])),
        )
        for r in rows
        if r.get("name") and str(r["name"]).upper() not in _HIDDEN_SCHEMAS
    ]


def list_objects(conn: Connection, database: str, schema: str) -> list[ObjectNode]:
    rows = _show(conn, f"SHOW OBJECTS IN SCHEMA {qualify(database, schema)}")
    out: list[ObjectNode] = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        kind_raw = str(r.get("kind") or "TABLE").upper()
        kind = VIEW if "VIEW" in kind_raw else TABLE
        out.append(
            ObjectNode(
                name=str(name),
                kind=kind,
                detail=kind_raw.title(),
                path=(database, schema, str(name)),
            )
        )
    out.sort(key=lambda n: n.name.lower())
    return out


def list_columns(conn: Connection, database: str, schema: str, table: str) -> list[ObjectNode]:
    """Columns and types for a table or view (B2)."""
    fqn = qualify(database, schema, table)
    rows = _show(conn, f"SHOW COLUMNS IN TABLE {fqn}")
    return [
        ObjectNode(
            name=str(r["column_name"]),
            kind=COLUMN,
            detail=_column_type(r),
            path=(database, schema, table, str(r["column_name"])),
        )
        for r in rows
        if r.get("column_name")
    ]


def _column_type(row: dict[str, Any]) -> str:
    """``SHOW COLUMNS`` returns the type as a JSON blob in ``data_type``."""
    import json

    raw = row.get("data_type")
    if not raw:
        return ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return str(raw)
    if not isinstance(parsed, dict):
        return str(raw)
    base = str(parsed.get("type", "")).upper()
    if base in {"FIXED", "NUMBER"} and "precision" in parsed:
        base = f"NUMBER({parsed['precision']},{parsed.get('scale', 0)})"
    elif base == "TEXT" and parsed.get("length"):
        base = f"VARCHAR({parsed['length']})"
    elif base == "REAL":
        base = "FLOAT"
    if parsed.get("nullable") is False:
        base += " NOT NULL"
    return base


# --------------------------------------------------------------------------
# Statements the context menu builds (B4)
# --------------------------------------------------------------------------


def preview_sql(database: str, schema: str, table: str, limit: int = 100) -> str:
    """``SELECT`` for the preview action."""
    return f"SELECT * FROM {qualify(database, schema, table)} LIMIT {int(limit)}"


def select_sql(
    database: str,
    schema: str,
    table: str,
    columns: Sequence[str] | None = None,
) -> str:
    """A ``SELECT`` to drop into the editor.

    Column names are listed when the browser has already loaded them, which
    saves retyping them; otherwise this falls back to ``*``.
    """
    fqn = qualify(database, schema, table)
    if not columns:
        return f"SELECT *\nFROM {fqn}"
    projected = ",\n       ".join(quote_ident(c) for c in columns)
    return f"SELECT {projected}\nFROM {fqn}"


#: ``GET_DDL`` needs the object's type as a string; views and tables differ.
_DDL_KINDS = {TABLE: "TABLE", VIEW: "VIEW", SCHEMA: "SCHEMA", DATABASE: "DATABASE"}


def get_ddl_sql(kind: str, *parts: str) -> str:
    """``GET_DDL`` for a database, schema, table or view.

    The object name is passed as a *string literal*, not an identifier, so it
    is quoted as one -- and it must carry its own identifier quoting inside
    that literal for mixed-case names to resolve.
    """
    object_type = _DDL_KINDS.get(kind, "TABLE")
    return f"SELECT GET_DDL({quote_literal(object_type)}, {quote_literal(qualify(*parts))})"
