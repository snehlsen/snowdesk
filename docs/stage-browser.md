# Stage browser with PUT / GET

**Feature specification, extends [spec.md](spec.md)**
Status: Draft, decisions settled (§11)

---

## 1. Summary

A way to see what is sitting in a Snowflake stage and move files in and out
of it without writing `LIST`, `PUT` and `GET` by hand. The sidebar gets a
second tab, **Stages**, next to **Objects**. It holds a tree of databases,
then schemas, then stages, then the folders and files inside each stage.
Files dragged in from Finder are uploaded with `PUT`, and the selected files
can be downloaded with `GET`.

spec.md §2 lists this as a v1 non-goal, and §15 lists it as a v2 idea. This
document is that v2 item.

## 2. Scope

### In

- Named internal stages, the user stage `@~` and table stages `@%t`: list,
  navigate, upload, download.
- External stages (S3, GCS, Azure): **list only**. `PUT`/`GET` do not work
  on them, so the UI says why instead of failing.
- Actions that generate SQL into the editor, such as `COPY INTO`, querying a
  file with `SELECT $1 …` and the stage's DDL.
- Every transfer is logged in Messages and recorded in History, the same as
  any statement (H1).

### Out

- Creating, altering or dropping stages. That is DDL, and the editor already
  handles it.
- Directory tables, `REFRESH`, and pre-signed URL generation.
- Previewing a file's contents in the tab. The generated `SELECT $1` covers
  this for now (§4, ST11).
- Syncing a local folder in either direction.

## 3. Scenarios

| # | Scenario |
|---|----------|
| T1 | Switch the sidebar to Stages, expand `RAW ▸ PUBLIC ▸ LANDING`, and see its files with their sizes, grouped into folders by `/`. |
| T2 | Drag three CSVs from Finder onto `LANDING/2026-09/`, watch them upload with per-file progress, and see them appear as `.csv.gz`. |
| T3 | Select a folder in the stage, choose Download, pick `~/Downloads`, and get the same folder structure on disk. |
| T4 | Right-click an uploaded file and choose Generate COPY INTO, which opens a statement in the editor that needs only the table name filled in. |
| T5 | Start a 2 GB upload by mistake, press Stop, and have it stop at the next file boundary, with a message saying which files made it. |
| T6 | Open an S3 external stage, browse it, and see that Upload and Download are disabled, with a tooltip explaining why. |

## 4. Functional requirements

| ID | Requirement | Pri |
|----|-------------|-----|
| ST1 | A **Stages** sidebar tab beside Objects. Its tree has the user stage `@~` first, then databases, schemas (these two levels share the object browser's cache) and stages (`SHOW STAGES IN SCHEMA`), each level loaded when expanded. | P0 |
| ST2 | Expanding a stage lists its files (`LIST @stage`) as tree children. Columns are Name and Size. Last modified and MD5 go in the tooltip. | P0 |
| ST3 | Show virtual folders by splitting paths on `/`, as expandable tree nodes. | P0 |
| ST4 | Cap the listing at the row cap (R4), and say so when the cap is hit, the same way the grid does. | P0 |
| ST5 | Upload from an Upload… button (file picker) or by dropping files or folders from Finder. Dropped folders keep their structure under the current prefix. | P0 |
| ST6 | Download the selected files or folders to a chosen local folder, keeping stage folder structure (see §7.4). | P0 |
| ST7 | Show per-file and total progress for transfers in a strip at the bottom of the Stages tab, and one line per file in Messages (uploaded, skipped, failed). | P0 |
| ST8 | Stop a transfer. It stops between files, not mid-file (§7.3). | P0 |
| ST9 | Ask Replace / Skip / Cancel before overwriting, both when an upload would replace a stage file and when a download would replace a local one. | P0 |
| ST10 | Disable transfers on external stages, with the reason shown. Listing still works. | P0 |
| ST11 | Context menu: copy stage path, insert `@db.schema.stage/path` into the editor, generate `COPY INTO`, generate `SELECT $1 … FROM @stage/file`, show DDL. | P1 |
| ST12 | Table stages: **Show Table Stage** in the Objects tree's table context menu switches to the Stages tab and adds and expands an `@%table` node. | P1 |
| ST13 | Delete the selected files or folders (`REMOVE`) after a confirmation that lists what will go. | P0 |
| ST14 | A filter box over the Stages tree, like the Objects filter (B5), applied to loaded nodes. | P1 |
| ST15 | PUT/GET typed in the editor resolve relative `file://` paths against the tab's saved `.sql` file, not the app's working directory, which is `/` in the bundle. | P2 |

## 5. UI

```
┌──────────────────────────────┬────────────────────────────────────────────┐
│ [Objects] [Stages]      [⟳]  │  query1.sql ×  │ +                         │
│ [filter…]        [Upload…]   │  …editor…                                  │
│ ▸ @~  (user stage)           │                                            │
│ ▾ RAW                        │                                            │
│   ▾ PUBLIC                   │ ────────────────────────────────────────── │
│     ▾ LANDING        Internal│  Result 1 │ Messages │ History             │
│       ▾ 2026-09/     3 files │                                            │
│           orders_01.csv.gz 12.4 MB                                        │
│           orders_02.csv.gz 11.9 MB                                        │
│       ▸ raw/        41 files │                                            │
│     ▸ S3_EXPORTS     External│                                            │
│ ▸ ANALYTICS                  │                                            │
│ ──────────────────────────── │                                            │
│ ▓▓▓▓▓░░ 2 of 3 · orders_03  │                                            │
│ 61%                  [Stop]  │                                            │
└──────────────────────────────┴────────────────────────────────────────────┘
```

- The sidebar's existing Objects content goes into a `QTabWidget` with a
  second Stages page. Each page has its own filter and refresh. The selected
  page is remembered in `QSettings`.
- Upload… uploads into the selected stage or folder. It is disabled when the
  selection is not an internal stage or a folder inside one.
- Dropping onto a stage or folder uploads into it. Dropping onto a file
  uploads into that file's folder. While dragging, the drop target is
  highlighted.
- Context menu on a file or folder: Download… (⌘D), Delete…, Copy Stage Path,
  Insert Stage Path, Generate COPY INTO, Generate SELECT $1, and Refresh. On
  a stage, also Show DDL. Items that generate SQL go to the editor, and Show
  DDL opens a result tab, the same as the Objects menu.
- The progress strip at the bottom of the panel appears only while a
  transfer runs. When a transfer finishes, the affected stage node refreshes
  on its own.
- The sidebar is narrow, so sizes are compact (`12.4 MB`). Folders show a
  file count. Everything else goes in the tooltip.

## 6. SQL issued

| Purpose | Statement |
|---------|-----------|
| Stages in a schema | `SHOW STAGES IN SCHEMA db.schema` (columns `name`, `type` = `INTERNAL`/`EXTERNAL`, `url`) |
| Listing | `LIST @db.schema.stage/prefix/`, or `LIST @~/prefix/` / `LIST @%t/prefix/` |
| Upload one file | `PUT 'file:///abs/path/f.csv' @stage/prefix/ AUTO_COMPRESS=TRUE OVERWRITE=… PARALLEL=4` |
| Download one folder | `GET @stage/prefix/sub/ 'file:///local/sub/' PATTERN='…'` (see §7.4) |
| Delete | `REMOVE @stage/prefix/ PATTERN='…'` for a file, `REMOVE @stage/prefix/folder/` for a folder |

**Stage paths match by prefix, not by name.** `LIST`, `GET` and `REMOVE`
given `@s/data` also hit `data2.csv` and `data_old/…`. That is harmless for a
listing but destructive for `REMOVE`. So folders are always addressed with a
trailing `/`, and single files are addressed by their parent folder plus a
fully anchored `PATTERN` (`'^…$'`, regex-escaped). The confirmation for ST13
lists the files the statement will actually match, taken from the current
listing, not just the names that were clicked.

All of these are built by one tested module, `db/stages.py`, never by string
formatting in the UI. PUT and GET do not accept bind parameters, so paths are
quoted as literals: `'` and `\` are escaped, spaces are allowed, and the
`file://` URL is built from an absolute `Path`. Stage names use the existing
`qualify` helper. Prefixes are percent-free literal paths. A name containing
`'` or a newline is refused before anything runs.

## 7. Design

### 7.1 Listing

`LIST` returns every file under the prefix, however deep, and has no
pagination. Expanding a stage runs it once as an ordinary worker job
(`ListStageJob`) through `execute_async`, fetches pages up to the row cap,
and builds the whole folder subtree on the client from the paths. Expanding
folders inside it needs no further query. If the cap was hit, the stage node
says so. A folder whose contents were cut off by the cap runs its own
`LIST @stage/folder/` when expanded. Refresh on any node re-lists from that
node's prefix.

The cap is what keeps a stage with a million files from taking down the app.
If this turns out too crude, the server can do the grouping instead:
`SELECT … FROM TABLE(RESULT_SCAN(qid)) GROUP BY <next path segment>` returns
one row per folder. That needs a warehouse, so it is not the default.

### 7.2 Where transfers run

PUT and GET are synchronous in the connector (spec.md §7.3). The connector
fetches short-lived cloud credentials from Snowflake and then moves the bytes
itself, straight to or from S3, Azure or GCS. A large transfer holds its
thread for minutes, for the same reason CSV export does. **Where that thread
lives is open decision D2.** The recommendation is the export model: a
one-thread `ThreadPoolExecutor` on the worker, with its own cursor on the
shared connection. The job queue keeps serving queries and the browser while
the transfer runs.

This also puts a second side-thread on the one connection, which
REMAINING.md §2.3 already flags as untested. That question needs an answer
before this ships. If the answer is bad, a dedicated transfer connection
fixes both export and transfers.

### 7.3 Progress and cancellation

`cursor.execute` accepts `_put_callback` / `_get_callback`: a
`SnowflakeProgressPercentage` subclass that the connector creates per file
and calls with byte counts. A `QtProgress` subclass forwards
`(transfer_id, file, seen, size)` to a worker signal. Updates are throttled
to about 10 per second, as export does with its row counts.

The connector cannot cancel a transfer that is under way. So SnowDesk issues
**one PUT per file** and one GET per folder. Stop sets an event, and the
loop checks it between statements. Raising from the progress callback to
abort mid-file is possible, but it runs inside the cloud SDK's threads and
could leave partial uploads behind. Spike it before relying on it.

One PUT per file gives up the connector's own parallelism across files.
`PARALLEL=4` still applies to the chunks of a single large file. Uploading
many small files will be slower than a single wildcard PUT. Batching files
that share a directory into one `PUT 'file:///dir/*'` is an optimisation for
later, at the cost of coarser Stop.

### 7.4 Download layout

The connector's GET flattens everything to basenames. This is a documented
TODO in `file_transfer_agent.py`: `a/x.csv` and `b/x.csv` land on the same
local path and overwrite each other, with only a log warning. So one GET is
issued **per stage folder**, into a matching local subfolder, with a
`PATTERN` that matches only that folder's direct children. Local collisions
are checked before the first GET, so the Replace/Skip prompt (ST9) comes up
front, not halfway through.

### 7.5 Upload options

Uploads keep Snowflake's default of `AUTO_COMPRESS=TRUE` (D3). A plain file
is gzipped on the way up, so `orders.csv` lands as `orders.csv.gz`. A file
that is already compressed (`.gz`, `.bz2`, `.zst`, `.parquet`, `.orc` and
the like) is uploaded as it is. This has three consequences:

- **The overwrite check must predict the stage name.** Before asking
  Replace/Skip (ST9), `orders.csv` is compared against `orders.csv.gz` on the
  stage, not against `orders.csv`. The prediction mirrors the connector's
  compression detection, and a PUT result whose `target` differs from the
  prediction is logged, so a wrong guess shows up.
- **Downloads come back compressed.** GET does not decompress, so
  `orders.csv.gz` arrives as `orders.csv.gz`. That is correct, but the
  download sheet says it once, so nobody expects `orders.csv`.
- The Messages line shows `orders.csv → orders.csv.gz (12.4 MB → 3.1 MB)`
  using the PUT result's `source_size` and `target_size`.

`OVERWRITE` is always sent explicitly. It is `TRUE` only for files the user
said to replace in the ST9 prompt. Otherwise Snowflake reports `SKIPPED`,
and Messages shows that too. A per-upload compression toggle, or a setting
for it, can be added later if needed.

### 7.6 New pieces

| Where | What |
|-------|------|
| `db/stages.py` | `list_stages`, `list_files`, and the `put_sql` / `get_sql` / `remove_sql` / `copy_into_sql` builders with quoting; `QtProgress` |
| `db/worker.py` | `ListStagesJob` (via BrowseJob path), `ListStageJob`, `TransferJob` on the transfer pool; signals `stage_listed`, `transfer_progress`, `transfer_file_done`, `transfer_finished`, `transfer_failed` |
| `controllers/stages.py` | Per-stage listing cache and transfers in flight. Maps drops and selections to jobs. Reuses `BrowserController` for the database and schema levels |
| `ui/stage_tree.py` | The Stages tree, drop target, context menu, and progress strip |
| `ui/main_window.py` | Sidebar becomes a `QTabWidget` (Objects, Stages). Adds a Show Table Stage hook from `ObjectTree` |
| `ui/dialogs.py` | `ask_download_folder`, `ask_replace_files`, `confirm_remove`, all behind the existing modal seam |

## 8. Error handling

| Situation | Behaviour |
|-----------|-----------|
| No privilege to `LIST` / `READ` / `WRITE` | The error goes to Messages (code, message, query ID), and the node shows a disabled "Could not list" child, the same as Objects does. |
| External stage | Listing works. Upload, Download and drop are disabled, with a tooltip saying transfers need an internal stage. |
| One file fails mid-batch | That file is marked failed and the rest continue. The summary line reads "3 uploaded, 1 failed". |
| Stop pressed | The current file finishes. Remaining files are marked "not started". This is a neutral status, not an error. |
| Session lost during transfer | Same reconnect strip as today. The transfer is reported as interrupted, with which files completed. |
| Local file unreadable or disappears | Checked before PUT. Reported per file. |
| Listing hits the cap | The footer says so and suggests narrowing the prefix. |
| Quit with a transfer running | Ask: Stop and Quit / Keep Running. Same pattern as an open transaction. |

## 9. Testing

**Unit tests:** every SQL builder with hostile names (quotes, spaces,
unicode, backslashes, a leading `@`), folder grouping from flat `LIST`
paths, pattern generation for the per-folder GET, and collision detection.

**Fake connection:** teach it `LIST` and a fake PUT/GET that calls the
progress callback and writes or reads a temporary directory. **Note:** the
fake has been wrong before. It populated `description` eagerly, which
shipped a real bug. Model PUT's actual result shape (`source`, `target`,
`source_size`, `target_size`, `source_compression`, `target_compression`,
`status`, `message`) from a real run, not from memory.

**UI tests:** switch to the Stages tab, expand a stage and folders, drop files using a
`QMimeData` with URLs, stop mid-batch, and the Replace prompt through its
seam. Also add the new actions to the action sweep.

**Integration tests:** create a temporary stage in the throwaway schema,
then PUT, LIST, GET into a temporary directory, compare bytes, REMOVE, and
check that `a/x.csv` and `b/x.csv` both survive a download.

## 10. Plan

| Step | Days |
|------|------|
| `db/stages.py` builders and tests; `LIST` and tree nodes | 1 |
| Stages sidebar tab: tree, folder grouping, filter, context menu | 1.5 |
| Transfers: pool, per-file loop, progress, Stop, collisions | 1.5 |
| Drag and drop, dialogs behind seams, action sweep | 0.5 |
| Integration tests against a real account, plus REMAINING §2.3 | 0.5 |
| **Total** | **~5** |

## 11. Decisions

Settled on 2026-09-24.

| # | Question | Decision |
|---|----------|----------|
| D1 | Where stages appear | Their own **Stages** sidebar tab beside Objects, holding both the stages and their contents (§5). |
| D2 | Where transfers run | A side thread on the shared connection, the same as export (§7.2). This depends on answering REMAINING §2.3 first. |
| D3 | `AUTO_COMPRESS` on upload | Snowflake's default: gzip. No toggle in v1 (§7.5). |
| D4 | Deleting files (`REMOVE`) | Included in v1, with a confirmation that lists what will go (ST13). |

## 12. Risks

| Risk | Mitigation |
|------|------------|
| Concurrent PUT/GET, export and statements on one connection misbehave | Answer REMAINING §2.3 first. Fall back to a dedicated transfer connection. |
| The GET flattening fix gets out of step with future connector versions | Integration test for the `a/x.csv` + `b/x.csv` case. Pin the connector. |
| `LIST` on huge stages is slow even when capped, since the server returns every row | Encourage prefixes. Keep the RESULT_SCAN grouping option in reserve. |
| Temporary stages are session-scoped | Only visible on the shared connection, which D2 uses. |
| PyInstaller misses the cloud SDK pieces PUT/GET need (boto3, azure) | Add a PUT/GET round trip to `--selftest`, or at least import checks. |
