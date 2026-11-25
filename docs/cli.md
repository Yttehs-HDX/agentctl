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
You can run this command after `mv OLD NEW`, so `OLD` does not need to exist.
`NEW` must exist. Session paths inside the workspace move to `NEW`; Git roots
outside the workspace stay unchanged. Existing Git worktrees are repaired before
the registry changes. Refuses a target that belongs to another workspace.
Directory moves are never detected automatically.

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
- `--task TEXT` appends the first message at launch. Passing it here instead of
  following with `send` means a booting agent cannot drop it.
- `--wait SECONDS` waits for the agent's first status report. Without it,
  `create` returns as soon as the terminal exists. The `ready` field says
  whether the agent has reported; `send` remains the hard delivery gate.

A failed `create` removes the row and any worktree or session directory it made.
If one of those cannot be removed, the error hint names the leftover path; no
later command could find it.

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
sessions. The deletion target is always `session_state_dir()/<session id>`,
never the recorded `native_session_dir`: a corrupted or tampered row can only
point at its own directory, and an id that would leave the state dir, or a
symlinked entry there, is reported and refused. The recorded path is compared,
and a disagreement is printed as a `note`. Sizes are reported before removal,
then the row is hard-deleted. Worktrees and git branches are never touched;
agent-global native stores are out of scope. `--dry-run` reports what would
happen without deleting anything. Human output:

```text
would prune 2 sessions, 2 directories, 4.0 KiB
note: session 'x': recorded path /elsewhere ignored; removing /state/sess_x
pruned 2 sessions, removed 2 directories, freed 4.0 KiB
kept: session 'y': refusing unsafe session id '../escape'
```

Exits 1 when any row was kept, including in `--dry-run`; the JSON payload
still lists every `errors` entry, plus the `notes` it ignored.

## leader

```text
agentctl leader attach  [--terminal T] [--native-session ID] [--takeover]
agentctl leader detach
agentctl leader status
```

- `attach` records where this workspace's Leader runs. `--terminal` defaults to
  `$HC_TERMINAL_ID`, which is how a Leader inside Herdr needs no arguments. An
  outside Leader passes no terminal and is recorded as such.
- A second `attach` from a different terminal is refused while the bound one is
  live; `--takeover` replaces it when you know it is gone. A takeover is
  last-writer-wins: it does not unlabel the previous terminal, so `info` lists
  that terminal under `orphans` until it exits or is removed.
- A terminal with a Worker role or session label cannot become the Leader.
  A terminal belonging to another workspace is also refused.
- `status` reports the binding and whether Herdr still calls that terminal live.
  There is no expiry; Herdr's terminal state is the truth.
- `detach` forgets the binding. The terminal keeps running.

## wait

```text
agentctl wait NAME [--timeout S]
agentctl wait --any [--timeout S]
agentctl wait --all [--timeout S]
```

`wait NAME` wraps `hc wait` for one session: it returns when the agent stops
working or exits. `--any` and `--all` require a live Leader terminal and wait
only on its Herdr children. Workers of a previous Leader and parentless
Workers are not included. Use `wait NAME` for those Workers. Group wait JSON
includes `scope: "leader_children"` and the `parent` address. An empty
`children` list is not a workspace-wide result. `reason: "status"` is a
finished turn. `reason: "timeout"` is a check-in, not an error.

## send

```text
agentctl send NAME TEXT [--force] [--wait] [--timeout S]
agentctl send NAME -                 # read stdin
```

Text is passed as one argv element, so quotes, `$VAR`, a leading `-` and
newlines are preserved. Herdr refuses a blocked agent unless `--force`, and a
finished terminal is an error. `--wait` asks Herdr to wait for the turn the send
started. `started: false` in the output means the text was typed but no turn
began, which is a doubt rather than a success.

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
