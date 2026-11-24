# agentctl

Workspace-scoped agent session manager on top of [Herdr Core](https://github.com/) (`hc`).

`agentctl` gives a Leader agent one CLI to own a pool of independent worker sessions:
create them, dispatch work to them without blocking, act on what they report, and
get the same pool, the same identities and the same unread events back after a
restart.

v0.2 turns the synchronous manager into an event-driven runtime: work is
persisted as a message before it is sent, an acknowledgement names the exact
message it received, and a local `agentctld` turns worker activity into events a
Leader can act on while it is doing its own work.

```text
Workspace (canonical dir) ── Leader identity (stable UUID)
   ├── researcher      omp      cwd=docs/       terminal t3   session 01a2...
   ├── reviewer        claude   cwd=.           terminal t6   session e78c...
   └── tester          codex    cwd=.           terminal t7   session unknown
```

## Install

```sh
uv tool install --from . agentctl      # or: uv pip install -e .
agentctl setup                          # checks hc, detects agents, writes config.toml
```

State lives in `~/.config/agentctl/` (`AGENTCTL_HOME` overrides):

```text
config.toml     # global configuration (TOML)
registry.db     # workspaces + sessions (SQLite, WAL)
sessions/       # per-session private dirs for agents that key sessions by directory
```

## Quick start

```sh
agentctl setup
agentctl config set default-agent omp

cd ~/projects/example
agentctl init                       # registers this directory as the workspace
agentctl integrations install omp   # the worker hook, under agentctl's own home

agentctl --json leader attach       # prints the lease token
export AGENTCTL_LEASE_TOKEN=...     # present it on every mutating command

agentctl create --name researcher --cwd docs --wait 45
agentctl --json send researcher "Map the module layout"   # returns at once
agentctl inbox list                 # what the worker has reported
agentctl read researcher
agentctl list
```

`--workspace` is no longer required: a command resolves the workspace from the
flag, then `AGENTCTL_WORKSPACE`, then the nearest registered root at or above the
current directory.

## What v0.2 adds

| Piece | Command | Guarantee |
| --- | --- | --- |
| Workspace discovery | every command | flag > env > cwd > nearest parent, canonicalized |
| Leader lease | `leader attach\|detach\|status\|heartbeat\|takeover` | one live Leader per workspace; a superseded token stops working |
| Message registry | `send`, `messages list\|get\|retry\|cancel` | persisted before sending; exact per-message ACK; `delivery_unknown` instead of a blind resend |
| Event log | `events list\|watch`, `inbox list\|ack` | deduped, ordered, survives a restart |
| Daemon | `daemon start\|stop\|status` | follows the Herdr Core event stream, dispatches and delivers |
| Integrations | `integrations list\|inspect\|install\|uninstall` | per-harness hooks with measured capabilities |
| Doctor | `doctor` | runtime, workspace, lease, daemon, agents and hooks in one pass |

Message states, in order: `queued`, `dispatching`, `dispatched`, `acknowledged`,
`running`, `completed`. `delivery_unknown` is a first-class outcome for a message
that may have arrived; it is never resent automatically.

## Supported harnesses

| Agent | Hook | Exact ACK | Idle wake |
| --- | --- | --- | --- |
| `omp` 19.1.0 | extension, loaded with `-e` | yes | yes |
| `pi` 1.5.2 | extension, loaded with `-e` | yes | yes |
| `claude` 2.1.327 | `--settings` fragment | yes | yes (`Stop` hook) |
| `codex`, `opencode` | none verified | no | no |

Codex and OpenCode stay out of scope: no verified hook, and codex emits no status
record for the runtime fallback either. Their sends are `delivery_unknown`.

See [docs/runtime-capabilities.md](docs/runtime-capabilities.md) for what was
measured, and [skills/agentctl-leader/SKILL.md](skills/agentctl-leader/SKILL.md)
for how a Leader is expected to drive a pool.

After the Leader exits and comes back, the same commands find the same workspace,
the same sessions and the same terminals.

## Commands

| Command | Purpose |
| --- | --- |
| `setup [--default-agent ID] [--force]` | One-time machine setup |
| `leader attach\|detach\|status\|heartbeat\|takeover` | Workspace Leader lease |
| `messages list\|get\|retry\|cancel` | The durable message registry |
| `events list\|watch` / `inbox list\|ack` | Event log and Leader inbox |
| `daemon start\|stop\|status` | The local `agentctld` service |
| `integrations list\|inspect\|install\|uninstall` | Harness hooks |
| `doctor` | One-pass health check |
| `init` | Create/load the workspace identity for `--workspace` (idempotent) |
| `info` | Workspace id, leader id, counts, default agent, runtime status |
| `rebind NEW` | Move this workspace identity to another directory |
| `list [--all]` | Sessions of this workspace |
| `create --name N --cwd DIR [--agent ID] [--mode shared\|worktree] [--base-ref REF] [--wait S]` | New worker session |
| `delete NAME [--keep-worktree]` | Kill the agent, clean the terminal, retire the record |
| `prune [--dry-run]` | Reclaim agentctl-owned state of soft-deleted sessions |
| `send NAME TEXT` / `send NAME -` | Send a message (stdin keeps bytes intact) |
| `keys NAME KEY [KEY...]` | Send named keys (answer trust/permission dialogs) |
| `interrupt NAME [--timeout S]` | Interrupt the agent (ctrl+c) |
| `read NAME [--lines N] [--follow]` | Screen + history |
| `status [NAME]` | Session or workspace status |
| `wait NAME [--timeout S]` | Wait until the agent stops working |
| `recover [NAME]` | Re-discover terminals, or relaunch from a native session id |
| `agents list\|inspect ID` | Catalog with measured capabilities |
| `config set\|get\|list` | `default-agent`, `session.default-mode`, `agents.<id>.enabled` |

Every command accepts `--json`. `--workspace` is optional; without it the
workspace comes from `AGENTCTL_WORKSPACE` or the nearest registered root at or
above the current directory.

## Status words

`starting`, `idle`, `working`, `waiting`, `completed`, `failed`, `stopped`,
`unknown`, `unreachable`.

Status comes from the agent's own Herdr Core status records. An agent that does not
report status is `unknown` — never guessed from the screen.

## Security boundary

`--cwd` must exist, resolve through symlinks to its real path, and stay inside the
workspace root. Agent commands come from the catalog only. `--cwd` is a scoping
constraint, not an OS sandbox.

## Docs

- [docs/runtime-capabilities.md](docs/runtime-capabilities.md) — measured Herdr Core behaviour
- [docs/architecture.md](docs/architecture.md) — modules, data model, recovery, lease, message lifecycle
- [docs/cli.md](docs/cli.md) — full CLI reference
- [docs/testing.md](docs/testing.md) — how to run the tests
