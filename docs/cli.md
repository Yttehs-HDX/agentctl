# CLI reference

Global options come before the command:

```text
agentctl [--workspace PATH] [--json] <command> [options]
```

- `--workspace PATH` — Leader workspace root. Optional. Resolution order:

  1. `--workspace PATH`
  2. `AGENTCTL_WORKSPACE`
  3. the registered workspace whose root is the current directory
  4. the nearest registered workspace above it (nested workspaces resolve to the
     innermost one)

  Every path is canonicalized with `realpath`, so a symlink and its target share
  one workspace. Discovery never creates a workspace and never guesses: with no
  registered root at or above the current directory the command fails with
  `workspace_not_initialized` and exit 4. `init` is the one command that may
  name an unregistered directory; without arguments it registers the current
  directory.
- `--lease-token TOKEN` — Leader lease token, also `AGENTCTL_LEASE_TOKEN`. Once
  a Leader has attached to the workspace, mutating commands must present it.
- `--leader-instance ID` — this Leader run's instance id, also
  `AGENTCTL_LEADER_INSTANCE`.
- `--json` — machine-readable output. Accepted before or after the command.
  Everything after a `--` terminator is caller data, so a literal `--json`
  message is sendable as `send NAME -- --json`. JSON objects carry
  `"schema": 1`; bumping that number means a field changed shape.

Exit codes: `0` ok, `1` generic/runtime error, `2` usage, `3` not found,
`4` setup or init missing, `5` conflict (duplicate name, …), `6` invalid
input/agent, `7` runtime failure, `8` blocked (human decision needed).

Errors print to stderr:

```text
error: <message>
hint:  <what to do>
```

with `--json` the same information is a JSON object:

```json
{"schema": 1,
 "error": {"type": "SessionNotFound", "code": "session_not_found",
           "message": "...", "hint": "...", "runtime_kind": "not_found"}}
```

Argument-parsing errors are JSON too when `--json` is active: type
`UsageError`, code `usage`, exit code 2.

## setup

```text
agentctl setup [--default-agent ID] [--force]
```

Checks `hc`, detects installed agents, writes `~/.config/agentctl/config.toml`,
creates `registry.db`, and prints the catalog. Re-running keeps the existing
config unless `--force` is given. `--default-agent` is optional; leaving it out
sets no default when several agents are installed.

## init

```text
agentctl --workspace PATH init
```

Creates the workspace identity (or reports the existing one). Idempotent: the
workspace id and leader id never change for the same canonical path.

## rebind

```text
agentctl --workspace OLD rebind NEW
```

Explicitly moves the workspace identity to another directory. The workspace id
and leader id are unchanged, sessions follow, and the old root stops resolving.
Refuses a target that belongs to another workspace. Directory moves are never
detected automatically.

## info

```text
agentctl --workspace PATH info
```

Workspace id, leader id, root, session totals, default agent, runtime version,
and any terminal labelled with the workspace id but absent from the registry.

## list

```text
agentctl --workspace PATH list [--all]
```

Live sessions of this workspace, with a runtime refresh. `--all` adds soft
deleted history (never refreshed from the runtime). `--no-refresh` skips the
per-session `hc get`, which is the difference between one call and N calls.

## create

```text
agentctl --workspace PATH create --name NAME --cwd DIR
        [--agent ID] [--mode shared|worktree] [--base-ref REF] [--wait SECONDS]
```

- `--name` unique among live sessions of this workspace.
- `--cwd` must exist, be a directory, resolve through symlinks, and stay inside
  the workspace root.
- `--agent` defaults to the workspace default, then the global default; if
  neither exists the command fails and asks for `--agent`.
- `--mode` defaults to `session.default_mode` (shipped as `shared`).
  `worktree` requires a git repository and creates
  `.agentctl/worktrees/<session id>` on branch
  `agentctl/<slug>-<session id prefix>`.
- `--wait SECONDS` waits for the agent's first status report. Without it,
  `create` returns as soon as the terminal exists. The `ready` field says
  whether the agent has reported; `send` remains the hard delivery gate.

Prints the session id, agent, cwd, mode, terminal address and readiness.

## delete

```text
agentctl --workspace PATH delete NAME [--keep-worktree]
```

Kills the agent, removes the terminal, applies the worktree policy, and soft
deletes the record. Never deletes worker files: a dirty worktree is kept. The
returned session has `hc_terminal_id: null`, matching the stored row.

## prune

```text
agentctl --workspace PATH prune [--dry-run]
```

Reclaims state owned by the registry for this workspace's soft-deleted
sessions: it removes each `native_session_dir` that is a direct child of
`session_state_dir()` (sizes are reported before removal), then hard-deletes
the row. A directory anywhere else is reported and kept, as is a row
whose directory could not be removed. Worktrees and git branches are never
touched; agent-global native stores are out of scope. `--dry-run` reports what
would happen without deleting anything. Human output:

```text
would prune 2 sessions, 2 directories, 4.0 KiB
pruned 2 sessions, removed 2 directories, freed 4.0 KiB
kept: session 'x': refusing to remove /elsewhere (outside ...)
```

Exits 1 when any row was kept, including in `--dry-run`; the JSON payload
still lists every `errors` entry.

## leader

```text
agentctl leader attach   [--instance ID] [--takeover]
agentctl leader detach
agentctl leader status
agentctl leader heartbeat
agentctl leader takeover [--instance ID]
```

- `attach` claims the lease and prints the instance id, epoch, token and expiry.
  Re-attaching the same instance is a renewal and keeps the epoch. A second
  instance is refused while the lease is live.
- `takeover` replaces a live or expired Leader and increments `leader_epoch`.
  Every token from an earlier epoch stops working: that is fencing.
- `heartbeat` renews. Safe to call from a Leader hook each turn.
- `detach` releases immediately instead of waiting for expiry.
- `status` reports the holder, the remaining time and the inbox depth.

Default lease is 120 s. A local process compares tokens; it is not an OS
sandbox, and the same Unix user can always read the database.

## messages

```text
agentctl messages list [--state STATE] [--session NAME] [--limit N]
agentctl messages get MESSAGE_ID
agentctl messages retry MESSAGE_ID
agentctl messages cancel MESSAGE_ID
```

States: `queued`, `dispatching`, `dispatched`, `acknowledged`, `running`,
`completed`, `failed`, `cancelled`, `delivery_unknown`. `messages get` prints
what the state means. `retry` accepts a `failed` or `queued` message and refuses
`delivery_unknown`: the text may already have reached the worker.

## events

```text
agentctl events list [--after EVENT_ID] [--event NAME] [--limit N]
agentctl events watch [--interval S]
```

`turn.completed` means the worker's turn ended, not that the task succeeded.

## inbox

```text
agentctl inbox list [--all]
agentctl inbox ack INBOX_OR_EVENT_ID
```

The inbox holds events a Leader must act on. `ack` is idempotent and stops
re-delivery. A worker whose harness has no wake path leaves its events here.

## daemon

```text
agentctl daemon start [--foreground] [--once] [--instance ID]
agentctl daemon stop
agentctl daemon status
```

`start` runs `agentctld` detached (state in `$AGENTCTL_HOME/daemon.json`, log in
`daemon.log`). It follows the Herdr Core event stream, renews the lease,
dispatches queued messages one per worker, drains the hook spool and delivers
Leader-relevant events. `--once` runs a single pass for diagnostics.

## integrations

```text
agentctl integrations list
agentctl integrations inspect AGENT
agentctl integrations install AGENT [--scope user|project]
agentctl integrations uninstall AGENT [--scope user|project]
```

`AGENT` is `omp`, `pi` or `claude`. Install is idempotent, writes only under
`$AGENTCTL_HOME/integrations/<agent>/`, and never edits the harness's own
configuration. `list` marks a harness `verified` only if its capabilities were
measured here; codex and opencode are listed as unsupported with the reason.

## doctor

```text
agentctl doctor
```

One pass over the runtime, the resolved workspace, the Leader lease, the daemon,
each agent's version drift and each installed hook. Exits 1 when any check
fails.

## send

```text
agentctl --workspace PATH send NAME TEXT [--force] [--wait SECONDS] [--sync]
agentctl --workspace PATH send NAME -          # read stdin
```

Default is asynchronous: the text is persisted, dispatched if the worker is
idle, and the command returns a message id at once. `--wait N` turns it into a
barrier that returns when the message's turn ends or the timeout expires
(`timed_out: true` is a check-in, not a failure). `--force` types even when no
ACK can be expected and records `delivery_unknown`. `--sync` keeps the v0.1
behaviour: type now and wait for a status report instead of queueing.

Text is passed as one argv element: quotes, `$VAR`, leading `-` and newlines are
preserved. `send NAME -` reads stdin and refuses a terminal: pipe the text
instead of expecting an agent to type it. Delivery must be confirmed: a session whose agent reports status
(codex/opencode do not) waits up to 15 s for a status report and then fails
with exit code 8 (`delivery_unconfirmed`) rather than typing blindly. An agent
that does not report status at all fails immediately. `--force` skips the
readiness proof and types anyway; the report then has `confirmed: false`. On a
dialog, `--force` submits the currently selected option, which can exit the
agent. Use `keys` to answer a dialog deliberately. Sending to a finished
session is an error, not a silent no-op.

## keys

```text
agentctl --workspace PATH keys NAME KEY [KEY...] [--json]
```

Sends named keys to a live terminal, which is how a Leader answers a trust or
permission dialog instead of waiting at `unknown`. Key names: `enter`, `esc`,
`tab`, `up`, `down`, `left`, `right`, `ctrl+c`, `alt+x`, `f5`, `a`. A name
starting with `-` is rejected at the service boundary. Output is
`{"session", "terminal", "status", "turn"}`; human output is
`sent keys to NAME (turn N)`. No readiness wait is performed.

## interrupt

```text
agentctl --workspace PATH interrupt NAME [--timeout S]
```

Sends the runtime's interrupt (`ctrl+c`) and reports
`{"session", "terminal", "key", "outcome"}`. Human output is
`NAME: ctrl+c -> outcome` with `-` for a missing value. No readiness wait is
performed; a finished terminal is an error.

## read

```text
agentctl --workspace PATH read NAME [--lines N] [--follow]
```

Prints the screen plus `N` history lines (default 50). When HC can only show the
screen (agent busy or full-screen app), a `[agentctl] partial: screen only (…)`
line marks it. `--follow` reprints while the content changes until the session
is no longer live; it cannot be combined with `--json`, because it never ends
and emits text snapshots.

## status

```text
agentctl --workspace PATH status [NAME]
```

One session or the whole workspace, refreshed from the runtime:
`starting`, `idle`, `working`, `waiting`, `completed`, `failed`, `stopped`,
`unknown`, `unreachable`. `--no-refresh` returns the stored status only.

## wait

```text
agentctl --workspace PATH wait NAME [--timeout S]
```

Waits until the agent stops working, exits, or the timeout expires
(`ended: false` — a check-in, not a failure). Default timeout 180 s.

## recover

```text
agentctl --workspace PATH recover [NAME]
```

For each session: attach to the live terminal (by persisted address, then by
label), or relaunch from the native session id when the terminal is gone.
Results: `attached`, `resumed`, `unrecoverable` (status `unreachable`, record
kept).

## agents

```text
agentctl agents list
agentctl agents inspect ID
```

`list` shows every configured agent: enabled, installed, whether the installed
version matches the release capabilities were measured on, whether it reports
status words, and whether resume is supported. `inspect` adds version,
`version_verified`, session-id strategy, resume verification and notes. A
version mismatch is a warning, never a refusal. `setup` marks such an agent
`installed (unverified)`.

## config

```text
agentctl config set default-agent omp
agentctl config set default-agent ""            # clear the default
agentctl config set session.default-mode worktree
agentctl config set agents.codex.enabled false
agentctl config get default-agent
agentctl config list                            # whole file as TOML
```

Supported keys: `default-agent`, `session.default-mode`,
`agents.<id>.enabled`.

## version

```text
agentctl version
```
