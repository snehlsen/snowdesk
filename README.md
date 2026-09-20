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

All **P0** requirements from the spec are implemented, plus milestones M0-M6.

| Area | Done | Not yet |
|------|------|---------|
| Connections | C1-C6 | C7 role/warehouse override, C8 edit connections |
| Query execution | Q1-Q6, Q7 (`QUERY_TAG`), Q8 (tab per result) | Q9 Snowsight link |
| Results | R1-R8 | R7 filtering (sort only), R9 Parquet/XLSX |
| Object browser | B1-B5 | — |
| Editor | E1, E2, E3 | E4 autocompletion |
| History | H1, H2 | — |
| Preferences | S1 | — |

### Keyboard shortcuts

| | |
|---|---|
| ⌘↩ / ⌘⇧↩ | run the statement under the cursor (or selection) / run everything |
| ⌘. | cancel the running statement, server-side |
| ⌘T / ⌘W | new editor tab / close tab |
| ⌘O / ⌘S / ⌘⇧S | open a `.sql` file / save / save as |
| ⌘⇧C | copy selected cells with headers |
| ⌘E | export the full result to CSV |
| ⌘I | show or hide the cell detail pane |
| ⌘, | settings |

Clicking a column header sorts the rows currently loaded. That is a
client-side sort, not a re-query, so the grid says as much whenever the result
is not fully fetched — otherwise a sorted page looks like an ordered answer to
a question nobody asked the database.

**⌘E exports the whole result**, not the rows on screen. It re-reads the result
by query id rather than draining the grid's cursor, so the export is complete
even when the row cap stopped the grid, the grid stays usable, and it can be
repeated. Rows are streamed a page at a time and never accumulated, so
exporting a million rows costs the same memory as exporting a thousand. The
export runs off the job queue, so queries and the object browser keep working
while it writes, and it can be cancelled.

**View ▸ Appearance** switches between Follow System, Light and Dark. The choice
is remembered, and with Follow System the window changes with macOS while
SnowDesk is running.

If the session expires or the network drops, SnowDesk marks the connection
dead and shows a strip offering one-click Reconnect. Editor tabs, their
contents and the messages log are left alone — only the connection is gone.

Right-click a node in the object browser to preview 100 rows, generate a
`SELECT` (with the column list, once columns have been loaded), copy or insert
the qualified name, show `GET_DDL`, or refresh that node. Preview and DDL open
a result tab and leave the editor alone.

Editor tabs are autosaved every few seconds to
`~/Library/Application Support/SnowDesk/session.json`, so unsaved work survives
a relaunch. A tab backed by an unmodified file stores only its path and is
re-read from disk, so editing a file outside SnowDesk is picked up.

## Logging and privacy

Logs go to `~/Library/Logs/SnowDesk/snowdesk.log`. Only SnowDesk's own records
are kept at INFO; the connector and the boto3 and urllib3 it depends on are
pinned to WARNING, since an ordinary connect writes several lines about
credential lookups and HTTP pools. Run with `--verbose` to see everything,
which is what you want when diagnosing connector trouble.

SnowDesk also opts out of the connector's platform detection, which otherwise
probes cloud metadata endpoints and calls AWS STS with whatever credentials it
finds, then reports the result to Snowflake at login. Setting
`SNOWFLAKE_DISABLE_PLATFORM_DETECTION` yourself overrides this either way.

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
