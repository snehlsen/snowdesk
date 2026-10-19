# snowflake-connector-python 5.0: migration assessment

Status: Assessment only; nothing upgraded. Checked 2026-10-19 against 5.0.0.

---

## 1. Summary

5.0.0 is a rewrite. The Python package is now a thin layer over a native
core written in Rust (`snowflake/connector/_core/sf_core_python.*.so`, the
"universal core", source at github.com/snowflakedb/drivers). The runtime
dependencies are only cryptography, protobuf, pytz and tomlkit. keyring,
requests, urllib3, boto3, pyOpenSSL, pyjwt and certifi are all gone, and so
is the `secure-local-storage` extra.

The public DB-API surface SnowDesk uses survived. Most of the internals it
reaches into did not. The test suite already shows this: against 5.0.0,
597 tests pass and 5 fail. All 5 failures are in `tests/unit/test_selftest.py`,
and `--selftest` reports 6/9 checks.

**Recommendation: no-go for now; revisit as a deliberate migration.** Nothing
here is blocked outright, but two things can only be settled against a real
account: the Keychain token cache (C4) and how cancels and network failures
are classified (§3.2). Today they would fail silently. A user would see a
browser window on every connect, or a cancel would show up as "connection
lost". Do the migration on its own branch once the integration tests (§13 of
spec.md, REMAINING.md 1.1) can be run, and stay on 4.8.x until then.

## 2. Item by item

| # | Where | 5.0 status | Verdict |
|---|-------|------------|---------|
| 1 | `db/session.py` `connect()` kwargs | `connection_name`, `client_session_keep_alive`, `client_store_temporary_credential`, `session_parameters`, `password`, `passcode` are all accepted (`connection_config.py`, `_internal/connection_config_mixin.py`). `private_key_file_pwd` is a deprecated rewrite to `private_key_password` and emits a `DeprecationWarning`. | Needs a small change |
| 2 | C4: SSO/MFA token cache in the Keychain | Python's `token_cache` module is gone. Caching now happens in the core: it carries `sf_core/src/token_cache/keyring_cache.rs` (the Rust `keyring` 3.6.3 crate, linked against `security-framework`'s `os/macos/keychain.rs`) and a `file_cache.rs` fallback (`credential_cache_v2.json` under `SF_TEMPORARY_CREDENTIAL_CACHE_DIR` / `XDG_CACHE_HOME`). `client_store_temporary_credential` still gates it, defaults to `True`, and also absorbs the old `client_request_mfa_token`. | Very likely works; must be verified with a real SSO login |
| 3 | `selftest._check_token_cache` | Imports `keyring` and `snowflake.connector.token_cache`, and neither exists. There is no Python-visible object left to inspect, so the check has to be rewritten or reduced to a version/feature probe. | Needs changes |
| 4 | `selftest._check_arrow` | `nanoarrow_arrow_iterator.PyArrowRowIterator` is gone. The compiled reader is now `_internal/arrow_stream_iterator.cpython-312-darwin.so`. | Needs changes |
| 5 | `selftest._check_transfer` | `file_transfer_agent`, `encryption_util` and the `*_storage_client` modules are gone. PUT/GET now run in the core (`sf_core/src/file_manager/*`). | Needs changes |
| 6 | `app.py` `SecretDetector` | `secret_detector` is gone. The `ImportError` fallback is there, so the verbose log still works but is unscrubbed by SnowDesk. The core redacts its own login requests ("secrets redacted"), and core logs arrive through a `snowflake.connector._core` stdlib logger. The noisy loggers named in `setup_logging` (botocore, urllib3) no longer exist. | Needs changes |
| 7 | `db/splitter.py` `util_text.split_statements` | Kept as a backward-compatibility re-export of `_internal.text_utils.split_statements`. The splitter tests pass. | Works |
| 8 | `ui/about.py`, `selftest._check_connector` | `snowflake.connector.__version__` exists (`5.0.0`). | Works |
| 9 | `db/runner.py`, `db/results.py` cursor and connection API | `execute`, `execute_async`, `get_results_from_sfqid`, `fetchone/many/all`, `description`, `sfqid`, `rowcount`, `get_query_status_throw_if_error`, `is_still_running` and `constants.QueryStatus` / `FIELD_ID_TO_NAME` are all present. Behaviour against a server is untested. | Works (by signature) |
| 10 | `db/errors.py` error classification | See §3.2. Client-side errnos changed. | Needs changes; verify live |
| 11 | `pyproject.toml` | The `[secure-local-storage]` extra must go (uv warns that it does not exist). Both `filterwarnings` entries name deprecations inside 4.x code that is gone, so drop them. | Needs a small change |
| 12 | `packaging/snowdesk.spec` | `snowflake.connector.snow_logging`, `keyring` and `collect_data_files("certifi")` name things that no longer ship. `collect_submodules("snowflake.connector")` still covers the Python side, and the two native `.so` files are collected as binaries of a collected package. TLS roots now come from `rustls-native-certs`, which reads the macOS trust store, so certifi is not needed. Needs a real bundle build plus `--selftest` on it. | Needs changes; verify by building |

## 3. Details

### 3.1 Token cache (C4)

The evidence is the native core's own strings: `token_cache/keyring_cache.rs`,
service `SnowflakeTokenCache`, and the Rust `keyring` crate built with macOS
Keychain support. On macOS that is the same store as before, but
written by different code, so assume:

- tokens cached by 4.x are probably not read by 5.0 (new key layout), which
  costs every user one browser login after the upgrade;
- the "slow auth" heuristic in `SnowflakeSession` (`BROWSER_PROMPT_SECONDS`)
  is the only in-app signal if caching silently stops working.

Verify: connect twice with an `externalbrowser` connection and confirm the
second connect opens no browser, then look for the new item in Keychain
Access.

### 3.2 Errors

`_internal/errorcode.py` keeps a subset of the old numbers.
`_internal/error_kinds.py` maps core error kinds to exceptions. If no server
`vendor_code` is present, `errno` is taken from `KIND_TO_ERRNO`, or else falls
back to the raw kind number (a small integer).

| SnowDesk relies on | In 5.0 |
|--------------------|--------|
| `251006` no password (`needs_password`) | Still defined |
| `250002` connection closed | Still defined |
| `250003` failed to send request | **Gone.** Network trouble is `ERROR_KIND_IO` → `OperationalError`, errno `4` |
| `253001`–`253008` PUT/GET errors (`_FILE_TRANSFER_ERRNOS`) | Only `253006` (file not found) and `253007` (compression) remain. A failed upload is likely `OperationalError` with errno `4` or `15` (stage binding) |
| `604`/`606` and message markers for cancel | Server cancels should keep vendor code 604. A client-side cancel is `ERROR_KIND_CANCELLED` → **`OperationalError`**, errno `5` |
| `390xxx` server codes (wrong password, MFA, session expired) | Server codes come through as `vendor_code`, so most likely unchanged |

The risk: `is_session_lost` treats any `OperationalError` as a lost session.
In 5.0 that would include a client-side cancel whose message lacks the cancel
markers, and any PUT/GET failure outside `253006`/`253007`. Both would mark a
healthy connection dead. The fix is to recognise `errno == 5` (cancelled) and
transfer failures explicitly, preferably by checking against the 5.0 constants
when the connector is importable.

## 4. Changes a migration needs

1. `pyproject.toml`: `snowflake-connector-python>=5.0`, drop the extra, drop
   both `filterwarnings` lines, and fix the comment about C4/keyring.
2. `db/session.py`: pass `private_key_password` instead of
   `private_key_file_pwd`.
3. `db/errors.py`: handle cancel (kind 5) and the new transfer and network
   errnos as in §3.2, with tests that build 5.0-shaped exceptions.
4. `selftest.py`: point the Arrow check at `_internal.arrow_stream_iterator`
   and the transfer check at whatever the core exposes (or drop it), and
   replace the keyring-based token-cache check. Update
   `tests/unit/test_selftest.py` to match.
5. `app.py`: decide whether SnowDesk still needs its own scrubber for
   `--verbose`, now that `SecretDetector` is gone. Update the logger names it
   quiets.
6. `packaging/snowdesk.spec`: drop `snow_logging`, `keyring` and certifi.
   Build the bundle and run `--selftest` on it.
7. Run the integration tests against a real account, plus the manual
   checklist items for SSO, MFA, cancel and PUT/GET.

## 5. How this was checked

- A throwaway venv with `snowflake-connector-python==5.0.0` (Python 3.12):
  importing every module SnowDesk names, inspecting signatures, and reading
  `connection_config.py`, `_internal/connection_config_mixin.py`,
  `_internal/errorcode.py` and `_internal/error_kinds.py`.
- `strings` over the native core for its token-cache and TLS components.
- The full test suite and `--selftest` from source against 5.0.0 with
  PySide6 6.11.2: 597 passed, 5 failed (all in the selftest), 1 skipped.
- Not checked: anything that needs a Snowflake account, and a PyInstaller
  build.
