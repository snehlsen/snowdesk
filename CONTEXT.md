# SnowDesk

A macOS desktop client for running SQL against Snowflake and browsing its objects and stages.

## Language

### Connecting

**Connection**:
A named entry in `connections.toml`: what the user picks from the connection list.
_Avoid_: profile, account, server

**Session**:
One live, authenticated login made from a Connection. Commit mode and any open Transaction belong to the Session, not the Connection.
_Avoid_: connection (for the live login)

**Lost session**:
A Session that ended without being asked to, such as a network drop or an expired token. It cannot be settled, so an open Transaction goes down with it. The user still sees it as "Connection lost".
_Avoid_: dropped connection, disconnect

**Session lifecycle**:
The life of one Session, from connecting (including any passphrase, password or MFA passcode prompt) through to it being ended or lost.

**Settle**:
To commit or roll back an open Transaction before something that cannot happen while it is open: ending its Session (Disconnect, Reconnect, quitting) or switching its Commit mode.

### Transactions

**Commit mode**:
Whether the Session commits each statement on its own (auto-commit) or waits for an explicit Commit (manual commit).

**Transaction**:
Uncommitted work open in a Session, as Snowflake reports it rather than as SnowDesk last asked for it.

### Editing

**Workspace**:
The set of open editor tabs and their unsaved contents, restored when SnowDesk relaunches.
_Avoid_: session, editor session
