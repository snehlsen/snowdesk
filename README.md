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

If key-pair auth points at an encrypted private key and the config has no
`private_key_file_pwd`, SnowDesk prompts for the passphrase and retries the
connect. The passphrase is kept in memory for that run only, so a reconnect
does not ask again, and is never written to disk.

## Status

All **P0** requirements from the spec are implemented, plus milestones M0-M4.

| Area | Done | Not yet |
|------|------|---------|
| Connections | C1-C6 | C7 role/warehouse override, C8 edit connections |
| Query execution | Q1-Q6, Q7 (`QUERY_TAG`), Q8 (tab per result) | Q9 Snowsight link |
| Results | R1-R5 | R6 CSV export UI\*, R7 sort/filter, R8 detail panel, R9 Parquet/XLSX |
| Object browser | B1, B2, B3, B5 (filter, refresh) | B4 context menu (preview, GET_DDL) |
| Editor | E1, E2, E3 | E4 autocompletion |
| History | H1, H2 | — |
| Preferences | — | S1 preferences dialog |

\* The streaming CSV writer (`util/export.py`) and batch iteration
(`ResultHandle.iter_batches`) are in place; only the menu action and file dialog
are missing. That is the one shortcut from spec section 8 that does nothing
yet (⌘E); it belongs with R6 in M6.

### Keyboard shortcuts

| | |
|---|---|
| ⌘↩ / ⌘⇧↩ | run the statement under the cursor (or selection) / run everything |
| ⌘. | cancel the running statement, server-side |
| ⌘T / ⌘W | new editor tab / close tab |
| ⌘O / ⌘S / ⌘⇧S | open a `.sql` file / save / save as |
| ⌘⇧C | copy selected cells with headers |

Editor tabs are autosaved every few seconds to
`~/Library/Application Support/SnowDesk/session.json`, so unsaved work survives
a relaunch. A tab backed by an unmodified file stores only its path and is
re-read from disk, so editing a file outside SnowDesk is picked up.

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
open dist/SnowDesk.app
```

For personal use, `uv run snowdesk` is enough. See spec section 12 for signing
and notarization; the build is ad-hoc signed, so Gatekeeper rejects it until it
is signed with a Developer ID and notarized.

### Verifying a build

A bundle that builds is not a bundle that runs: PyInstaller reports success
whether or not the connector's compiled pieces can actually be imported. Check
a build with:

```
./dist/SnowDesk.app/Contents/MacOS/snowdesk --selftest
```

It loads Qt and opens a window, imports the connector and its compiled Arrow
result reader, round-trips an encrypted RSA key, checks the CA store and the
history database, and reads the connection config. Add `--connection NAME` to
also open a session and run `SELECT CURRENT_VERSION()`. The same command works
from source (`uv run snowdesk --selftest`), so a failure tells you whether the
problem is the build or the environment.
