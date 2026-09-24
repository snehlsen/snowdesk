# Stage browser with PUT / GET

**Feature specification, extends [spec.md](spec.md)**
Status: Implemented; integration tests not yet run against a real account (§13)

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
| ST1 | A **Stages** sidebar tab beside Objects. Its tree has the user stage `@~` first, then every stage the role can see, grouped by database and schema, from one `SHOW STAGES IN ACCOUNT` sent the first time the tab is shown. Databases and schemas without stages do not appear. | P0 |
| ST2 | Expanding a stage lists its files (`LIST @stage`) as tree children. Columns are Name and Size. Last modified and MD5 go in the tooltip. | P0 |
| ST3 | Show virtual folders by splitting paths on `/`, as expandable tree nodes. | P0 |
| ST4 | Cap the listing at the row cap (R4), and say so when the cap is hit, the same way the grid does. | P0 |
| ST5 | Upload from an Upload… button (file picker) or by dropping files or folders from Finder. Dropped folders keep their structure under the current prefix. | P0 |
| ST6 | Download the selected files or folders to a chosen local folder, keeping stage folder structure (see §7.4). | P0 |
| ST7 | Show per-file and total progress for transfers in a strip at the bottom of the Stages tab, and one line per file in Messages (uploaded, skipped, failed). | P0 |
| ST8 | Stop a transfer. It stops between files, not mid-file (§7.3). | P0 |
| ST9 | Ask Replace / Skip / Cancel before overwriting, both when an upload would replace a stage file and when a download would replace a local one. | P0 |
| ST10 | Disable transfers on external stages, with the reason shown. Listing still works. | P0 |
| ST11 | Context menu: copy stage path, insert `@db.schema.stage/path` into the editor, generate `COPY INTO`, generate `SELECT $1 … FROM @stage/file`, and `DESCRIBE STAGE` (`GET_DDL` has no stage object type). | P1 |
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
│     ▾ LANDING                │  Result 1 │ Messages │ History             │
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
- Context menu, on an internal stage: Upload Files…, Download… (⌘D), Copy
  Stage Path, Insert Stage Path, Generate COPY INTO, Describe Stage and
  Refresh. A folder adds Delete… and drops Describe Stage. A file has
  Download…, Delete…, the path items, Generate COPY INTO and Generate SELECT.
  An external stage has no transfer or delete items at all. Items that
  generate SQL go to the editor. Describe Stage runs in a result tab, the
  same way Show DDL does in the Objects menu.
- The progress strip at the bottom of the panel appears only while a
  transfer runs. When a transfer finishes, the affected stage node refreshes
  on its own.
- The sidebar is narrow, so sizes are compact (`12.4 MB`) and always shown
  in full. Long names are shortened in the middle, as Finder does. Folders
  show a file count. Only external stages get a label ("External"). The full
  path, byte count, modified time and MD5 go in the tooltip.

## 6. SQL issued

| Purpose | Statement |
|---------|-----------|
| All stages | `SHOW STAGES IN ACCOUNT` (columns `name`, `database_name`, `schema_name`, `type` starting `INTERNAL` or `EXTERNAL`, `url`) |
| Listing | `LIST @db.schema.stage/prefix/`, or `LIST @~/prefix/` / `LIST @%t/prefix/` |
| Upload one file | `PUT 'file:///abs/path/f.csv' @stage/prefix/ AUTO_COMPRESS=TRUE OVERWRITE=… PARALLEL=4` |
| Download one folder | `GET @stage/prefix/sub/ 'file:///local/sub/' PATTERN='^(…)$'`, naming up to 50 files exactly (see §7.4) |
| Describe | `DESCRIBE STAGE db.schema.stage` |
| Delete | `REMOVE @stage/prefix/ PATTERN='…'` for a file, `REMOVE @stage/prefix/folder/` for a folder |

**Stage paths match by prefix, not by name.** `LIST`, `GET` and `REMOVE`
given `@s/data` also hit `data2.csv` and `data_old/…`. That is harmless for a
listing but destructive for `REMOVE`. So folders are always addressed with a
trailing `/`, and single files are addressed by their parent folder plus a
fully anchored `PATTERN` (`'^(…)$'`) naming the file's raw `LIST` name,
escaped for POSIX ERE. The confirmation for ST13 names each file, and says a
folder means everything in it along with how many files the tree has listed
there.

All of these are built by one tested module, `db/stages.py`, never by string
formatting in the UI. PUT and GET do not accept bind parameters, so paths are
quoted as literals. A location is left bare when it can be, and otherwise
quoted whole, which is how spaces and quoted identifiers get through. The
`file://` URL is built from an absolute path and escaped for `glob`, because
the connector globs a PUT source: `data[1].csv` would otherwise match
nothing, or the wrong file. A local or stage path containing `'`, `\` or a
control character is **refused**, not escaped. It would have to survive both
the SQL literal and the path the server hands back to the connector, and
nothing checks that round trip. The file is reported as failed, with a
reason, and the rest of the transfer goes ahead.

## 7. Design

### 7.1 Listing

`LIST` returns every file under the prefix, however deep, and has no
pagination. Expanding a stage runs it once as an ordinary worker job
(`ListStageJob`), synchronously like the browser's `SHOW`. It reads up to the
row cap and groups the paths into folders on the client. A folder's rows are
built only when it is opened, so a stage with a hundred thousand files does
not build a hundred thousand tree rows up front. Expanding folders needs no
further query. If the cap was hit, the stage says so in a notice row and
suggests refreshing a folder. Refresh on any stage or folder re-lists just
that prefix.

The cap is what keeps a stage with a million files from taking down the app.
If this turns out too crude, the server can do the grouping instead:
`SELECT … FROM TABLE(RESULT_SCAN(qid)) GROUP BY <next path segment>` returns
one row per folder. That needs a warehouse, so it is not the default.

### 7.2 Where transfers run

PUT and GET are synchronous in the connector (spec.md §7.3). The connector
fetches short-lived cloud credentials from Snowflake and then moves the bytes
itself, straight to or from S3, Azure or GCS. A large transfer holds its
thread for minutes, for the same reason CSV export does. Per D2, it follows
the export model: a one-thread `ThreadPoolExecutor` on the worker
(`snowdesk-transfer`), with its own cursor on the shared connection. Planning
runs there too, so a plan and the transfer after it stay in order. The job
queue keeps serving queries and the browser while the transfer runs. The
controller allows one transfer at a time, from planning through to the
finish.

This also puts a second side-thread on the one connection, which
REMAINING.md §2.3 already flags as untested. That question needs an answer
before this ships. If the answer is bad, a dedicated transfer connection
fixes both export and transfers.

### 7.3 Progress and cancellation

**Progress is per file, not per byte.** `cursor.execute` accepts
`_put_callback` / `_get_callback`, and the file transfer agent copies them
onto each file's metadata. In connector 4.7.5 no storage client ever calls
them. The strip therefore shows "Uploading 2 of 3 · orders_03.csv" and a bar
that advances by the bytes of each file as it completes. A single large file
shows no movement until it is done.

The connector cannot cancel a transfer that is under way, and there is no
callback to raise from. So SnowDesk issues **one PUT per file** and one GET
per folder. Stop sets an event, and the loop checks it between statements.
Files not reached are reported as "not started".

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
`PATTERN` naming that folder's files exactly (at most 50 per GET). Each
selected file or folder is listed again when the download is planned: the
tree may be stale or capped, and the download should take what is on the
stage now. Local collisions are checked before the first GET, so the
Replace/Skip prompt (ST9) comes up front, not halfway through.

**Server paths are not trusted as local paths.** An object store will
happily hold a key like `../../.zshrc`. Every download target is resolved and
refused unless it stays inside the chosen folder.

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
  `orders.csv.gz` arrives as `orders.csv.gz`. That is correct, but Messages
  says it once per download, so nobody expects `orders.csv`.
- The Messages line shows `orders.csv → orders.csv.gz (12.4 MB → 3.1 MB)`
  using the PUT result's `source_size` and `target_size`.

`OVERWRITE` is always sent explicitly. It is `TRUE` only for files the user
said to replace in the ST9 prompt. Otherwise Snowflake reports `SKIPPED`,
and Messages shows that too. A per-upload compression toggle, or a setting
for it, can be added later if needed.

### 7.6 New pieces

| Where | What |
|-------|------|
| `model.py` | `StageRef`, `StageFile`, `TransferPlan` and the progress, per-file result and summary types that cross the thread boundary |
| `db/stages.py` | Every statement builder and name check; `list_stages`, `list_files`, `build_tree`; `plan_upload`, `plan_download`, `plan_remove`; `run_transfer` |
| `db/worker.py` | `StagesJob` and `ListStageJob` on the job queue. `plan_upload`, `plan_download`, `start_transfer` and `stop_transfer` on the transfer pool |
| `controllers/stages.py` | One transfer at a time, from plan to finish. Records every PUT, GET and REMOVE in History |
| `ui/stage_tree.py` | `StageTree` (the tree and Finder drops) and `StagePanel` (filter, Upload…, progress strip, context menu). Dialogs are methods tests can replace: `ask_upload_files`, `ask_download_folder`, `ask_replace`, `confirm_remove` |
| `ui/main_window.py` | Sidebar becomes a `QTabWidget` (Objects, Stages) that remembers its page. `show_table_stage`, and `ask_quit_during_transfer` on close |

## 8. Error handling

| Situation | Behaviour |
|-----------|-----------|
| No privilege to `LIST` | The node shows the error as a red child row, the same as Objects does. A failed PUT, GET or REMOVE goes to Messages and History with its code and query ID. |
| External stage | Listing works. The menu has no transfer or delete items, Upload… is disabled, and drops are refused. |
| One file fails mid-batch | That file is marked failed and the rest continue. The summary line reads "3 uploaded, 1 failed". |
| Stop pressed | The current file finishes. Remaining files are marked "not started". This is a neutral status, not an error. |
| Session lost during transfer | The transfer stops and is reported as interrupted, listing which files completed and which were not started. The reconnect strip appears once the worker's next statement notices the session is gone. |
| Local file unreadable or disappears | Checked before PUT. Reported per file. |
| Listing hits the cap | A notice row says so and suggests refreshing a folder on its own. |
| Quit with a transfer running | Ask: Stop and Quit / Keep Running. Same pattern as an open transaction. |

## 9. Testing

**Unit tests:** every SQL builder with hostile names (quotes, spaces,
unicode, backslashes, a leading `@`), folder grouping from flat `LIST`
paths, pattern generation for the per-folder GET, and collision detection.

**Fake connection:** `tests/fakes.py` has a `FakeStages` store that answers
`SHOW STAGES`, `LIST`, PUT, GET and REMOVE against a temporary directory. It
flattens GET the way the connector does and reports statuses as an enum, not
text. The PUT and GET result shapes come from `file_transfer_agent.result()`
in connector 4.7.5. **Note:** the fake has been wrong before, so the
assumptions in §13 are checked only by the integration tests.

**UI tests:** switch to the Stages tab, expand a stage and folders, drop files using a
`QMimeData` with URLs, stop mid-batch, and the Replace prompt through its
seam. The panel's own actions get the same `checked`-argument sweep as the
window's.

**Integration tests:** create a temporary stage in the throwaway schema,
then PUT, LIST, GET into a temporary directory, compare bytes, REMOVE, and
check that `a/x.csv` and `b/x.csv` both survive a download. Written in
`tests/integration/test_snowflake.py`; not yet run.

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
| PyInstaller misses the modules PUT/GET load only when a file moves | `--selftest` imports the file transfer agent, the S3, Azure and GCS storage clients and the encryption code; the bundle passes it. They need nothing outside the connector and the standard library. |

## 13. Status

Built on the `stage-browser` branch. ST1 to ST14 are done. ST15 (resolving
relative `file://` paths typed in the editor) is not.

**Assumptions only the integration tests can confirm.** Nothing here has met
a real account yet. The fake encodes what the documentation and the connector
source say, which is exactly how it was wrong before:

- `LIST` on a named stage returns names prefixed with the stage's name in
  lower case (`landing/…`), while `@~` and `@%t` return bare paths.
  `relative_name` depends on this.
- `GET` and `REMOVE` match `PATTERN` against the same full name `LIST`
  shows, and treat it as an anchored POSIX regex.
- A whole location in single quotes (`'@DB.S."My Stage"/my folder/'`) is
  accepted by LIST, PUT, GET and REMOVE.
- `SHOW STAGES IN ACCOUNT` reports `type` starting `INTERNAL` or `EXTERNAL`.
- `DESCRIBE STAGE` works for any stage the role can list.
- A transfer on the side thread does not disturb a statement the worker is
  running at the same time (REMAINING §2.3).
