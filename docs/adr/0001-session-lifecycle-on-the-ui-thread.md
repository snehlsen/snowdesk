# The Session lifecycle lives on the UI thread; the worker only reports facts

The Session lifecycle module (`controllers/session_lifecycle.py`) runs on the UI thread and holds the one authoritative Session state (Disconnected, Connecting, Awaiting passphrase, Connected, Lost, Failed). It also owns passphrase memory, the last Connection for Reconnect, and Settle. The worker is still the only code that touches `SnowflakeSession`, but it stops making lifecycle decisions. It reports facts (connected, connect failed, Session lost, disconnected) and runs the jobs it is given.

We chose this because the lifecycle needs the user partway through (the passphrase prompt, and Settle before Disconnect, Reconnect or quit), and `closeEvent` needs an answer synchronously. Keeping the lifecycle in the worker left it split across two threads. That split produced six copies of "connected?", UI-thread reads of the worker's session, and correctness that depended on signal emission order and on construction order in `app.py`.

## Considered options

- **Lifecycle inside the worker**, with a cached copy on the UI side. Rejected: the prompts and Settle would still live in the window, so the lifecycle would stay split.
- **A Qt-free state machine as the module's interface**, with a thin Qt adapter. Rejected as the interface, but kept as an internal seam: the transition logic is Qt-free inside the module and tested directly.

## Consequences

- Lanes still classify their own failures with `errors.is_session_lost`, but they report a lost Session to this module instead of tearing down. The module ignores reports after the first.
- A failed Settle leaves the Session Connected, and the Disconnect, Reconnect or quit does not happen.
- Caches reset when the Session leaves Connected, not when it arrives, so nothing depends on the order subscribers run in.
