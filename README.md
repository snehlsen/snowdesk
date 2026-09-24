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

What SnowDesk does write — the query history at
`~/Library/Application Support/SnowDesk/history.db`, the autosaved editor
tabs beside it, and the log in `~/Library/Logs/SnowDesk` — is kept readable
only by you. History holds the text of every statement you run, which is
where credentials tend to appear in the open, so it is treated the way the
`snow` CLI treats `connections.toml`. Files left world-readable by an earlier
version are tightened the next time the app starts.

## Status

All **P0** requirements from the spec are implemented, along with milestones
M0-M6. M7 is partly done: a dropped session is detected and recoverable, and
logging is in place; the manual checklist, the first real run of the
integration tests, and signing and notarization are not.

| Area | Done | Not yet |
|------|------|---------|
| Connections | C1-C6 | C7 role/warehouse override, C8 edit connections |
| Query execution | Q1-Q6, Q7 (`QUERY_TAG`), Q8 (tab per result), query profile | Q9 Snowsight link |
| Results | R1-R6, R7 sort, R8 | R7 filtering, R9 Parquet/XLSX |
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
| ⌘C / ⌘⇧C | copy selected cells / copy with headers |
| ⌘E | export the full result to CSV |
| ⌘I | show or hide the cell detail pane |
| ⌘⇧P | query profile for the focused result |
| ⌘, | settings |
| ⌘R | reconnect |
| ⌘D | download the selected stage files (in the Stages sidebar) |

### Query profiles

⌘⇧P opens `GET_QUERY_OPERATOR_STATS` for the focused result in its own tab,
and Query ▸ Copy Query ID copies that result's query ID. The History tab's
context menu offers both for any statement it has recorded, which is where to
go once a run has closed the result tabs.

Both work from the query ID SnowDesk recorded when it ran the statement, and
that matters: statements go through `execute_async`, and the connector reads
their results back with `select * from table(result_scan('<id>'))` on the same
session. That wrapper is the session's *last* query, so `LAST_QUERY_ID()` and
the newest row of `QUERY_HISTORY` both point at it rather than at the statement
you ran, and profiling it shows a single RESULT_SCAN over a cached result. Use
the recorded ID — from the status bar, these menu items, or the `query_id`
column in `history.db` — not `LAST_QUERY_ID()`.

A query can also legitimately have no profile at all: Snowflake keeps operator
statistics only for queries that did work on a warehouse, so result-cache hits,
metadata-only queries, `SHOW`, DDL and statements that failed before execution
have none, and statistics are dropped after 14 days. SnowDesk says so in
Messages rather than opening an empty grid.

Clicking a column header sorts the rows currently loaded. That is a
client-side sort, not a re-query, so the grid says as much whenever the result
is not fully fetched — otherwise a sorted page looks like an ordered answer to
a question nobody asked the database.

Values that Excel and Sheets would run as a formula — anything starting `=`,
`+`, `-` or `@` — get a leading apostrophe when exported or copied, so opening
a result in a spreadsheet cannot execute what was in the table. The apostrophe
is visible in the file; turn it off under Settings → Exports if you need the
bytes verbatim.

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

### Stages

The sidebar's **Stages** tab lists every stage your role can see, grouped by
database and schema, with your user stage (`@~`) first. Expand a stage to see
its files, grouped into folders by `/`. To upload, drag files or folders onto
a stage or folder from Finder, or use Upload…. To download, right-click, or
press ⌘D, and pick a local folder. Delete is in the same menu and always
asks first. Right-clicking a table in Objects offers **Show Table Stage** for
its `@%table` stage. See [docs/stage-browser.md](docs/stage-browser.md).

A few things behave in ways worth knowing:

* **Uploads are gzipped**, as a plain `PUT` does. `orders.csv` lands as
  `orders.csv.gz`, files that are already compressed go up unchanged, and
  downloads come back exactly as stored.
* **Downloads keep folder structure.** GET on its own flattens everything
  into one directory, so `a/x.csv` and `b/x.csv` would overwrite each other.
  SnowDesk issues one GET per stage folder instead.
* **Stop takes effect between files.** The connector cannot interrupt a file
  mid-transfer, and progress advances a file at a time for the same reason.
* **Transfers run beside queries**, on their own thread, so the editor and
  browser keep working. One transfer runs at a time.
* **External stages can be browsed, not transferred.** PUT and GET only work
  on internal stages.
* **Some file names are refused.** A name containing `'`, `\` or a control
  character is reported as failed rather than passed to Snowflake. Rename
  the file.

Every PUT, GET and REMOVE is logged in Messages and recorded in History.

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
is signed with a Developer ID and notarized. No `.dmg` is produced yet — the
build stops at `dist/SnowDesk.app`.

### Application icon

Without an icon of its own the bundle shows PyInstaller's stock one. Convert a
square PNG, 1024x1024 or larger, and rebuild:

```
packaging/make_icon.sh path/to/logo.png
uv run pyinstaller packaging/snowdesk.spec --noconfirm
```

That writes `packaging/icon.icns`, which the spec file picks up when it exists.
macOS caches icons aggressively, so a rebuilt bundle in the same place may keep
showing the old one until it is moved, renamed, or the Finder is restarted.

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

## License

MIT — see [LICENSE](LICENSE).
