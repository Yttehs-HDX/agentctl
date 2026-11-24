# CLI reference

Global options come before the command:

```text
agentctl [--workspace PATH] [--json] <command> [options]
```

- `--workspace PATH` — Leader workspace root. Mandatory for `init`, `info`,
  `list`, `create`, `delete`, `prune`, `send`, `keys`, `interrupt`, `read`,
  `status`, `wait`, `recover`.
  It is never inferred from the current directory. The path is resolved with
  `realpath`, so a symlink and its target share one workspace.
- `--json` — machine-readable output. Accepted before or after the command.

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
{"error": {"type": "SessionNotFound", "code": "session_not_found",
           "message": "...", "hint": "...", "runtime_kind": "not_found"}}
```

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
deleted history (never refreshed from the runtime).

## create

```text
agentctl --workspace PATH create --name NAME --cwd DIR
        [--agent ID] [--mode shared|worktree] [--base-ref REF]
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

Prints the session id, agent, cwd, mode and terminal address.

## delete

```text
agentctl --workspace PATH delete NAME [--keep-worktree]
```

Kills the agent, removes the terminal, applies the worktree policy, and soft
deletes the record. Never deletes worker files: a dirty worktree is kept.

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

## send

```text
agentctl --workspace PATH send NAME TEXT [--force]
agentctl --workspace PATH send NAME -          # read stdin
```

Text is passed as one argv element: quotes, `$VAR`, leading `-` and newlines are
preserved. Delivery must be confirmed: a session whose agent reports status
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
is no longer live.

## status

```text
agentctl --workspace PATH status [NAME]
```

One session or the whole workspace, refreshed from the runtime:
`starting`, `idle`, `working`, `waiting`, `completed`, `failed`, `stopped`,
`unknown`, `unreachable`.

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

`list` shows every configured agent: enabled, installed, whether it reports
status words, whether resume is supported. `inspect` adds version, session-id
strategy, resume verification and notes.

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
