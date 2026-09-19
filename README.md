# SnowDesk

A lightweight macOS client for Snowflake: query editor, results grid, and schema
browser, using the connection config you already have for the `snow` CLI.
See [docs/spec.md](docs/spec.md) for the full specification.

## Running

```
uv sync
uv run snowdesk
```

SnowDesk reads connections from the same files the `snow` CLI uses, and accepts
either layout:

* `~/.snowflake/connections.toml`, with one `[connection_name]` table each;
* `~/.snowflake/config.toml`, with connections under `[connections.connection_name]`.

Both are merged, `connections.toml` winning on a name collision. Override the
directory with `SNOWFLAKE_HOME`. SnowDesk never reads or stores credentials
itself — the connector resolves those at connect time.

## Status

All **P0** requirements from the spec are implemented (milestones M0–M3, plus
E1, B1 and H1).

| Area | Done | Not yet |
|------|------|---------|
| Connections | C1–C6 | C7 role/warehouse override, C8 edit connections |
| Query execution | Q1–Q6, Q7 (`QUERY_TAG`), Q8 (tab per result) | Q9 Snowsight link |
| Results | R1–R5 | R6 CSV export UI\*, R7 sort/filter, R8 detail panel, R9 Parquet/XLSX |
| Object browser | B1, B2, B3, B5 (filter, refresh) | B4 context menu (preview, GET_DDL) |
| Editor | E1 | E2 tabs + autosave, E3 open/save, E4 autocompletion |
| History | H1, H2 | — |
| Preferences | — | S1 preferences dialog |

\* The streaming CSV writer (`util/export.py`) and batch iteration
(`ResultHandle.iter_batches`) are in place; only the menu action and file dialog
are missing.

## Development

```
uv run pytest            # unit + UI tests
uv run ruff check .
uv run ruff format .
uv run mypy
```

Integration tests need a real account and are opt-in — they create and drop a
throwaway schema per run:

```
SNOWDESK_IT_CONNECTION=default uv run pytest -m integration
```

## Packaging

```
uv run pyinstaller packaging/snowdesk.spec --noconfirm
```

For personal use, `uv run snowdesk` is enough. See spec section 12 for signing
and notarization.
