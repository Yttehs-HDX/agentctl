# agentctl

A thin convenience CLI over [Herdr Core](https://github.com/herdrdev/herdr-core)
(`hc`), made for Agents to use on themselves.

Herdr already runs programs in persistent terminals, gives them parents, sends
them messages, waits for their turns and reports their status. agentctl does not
re-implement any of that. It adds the three things an Agent needs to run a pool
of Workers without thinking about Herdr:

- **a Workspace** — a canonical directory with a stable identity;
- **a Leader binding** — where this workspace's Leader is, so a restart finds the
  same pool;
- **Worker identities** — named sessions mapped onto Herdr terminals.

```text
Workspace (canonical dir) ── Leader identity (stable UUID)
   ├── researcher   omp      cwd=docs/   terminal t3   session 01a2...
   ├── reviewer     claude   cwd=.       terminal t6   session e78c...
   └── tester       pi       cwd=.       terminal t7   session 01a9...
```

## Install

```sh
uv tool install --from . agentctl      # or: uv pip install -e .
agentctl setup                          # checks hc, detects agents, writes config.toml
```

State lives in `~/.config/agentctl/` (`AGENTCTL_HOME` overrides):

```text
config.toml     # global configuration (TOML)
registry.db     # workspaces, sessions, the Leader binding (SQLite, WAL)
sessions/       # per-session private dirs for agents that key sessions by directory
```

## Quick start

```sh
agentctl setup
agentctl config set default-agent omp

cd ~/projects/example
agentctl init                     # register this directory as the workspace

agentctl leader attach            # from the Leader's own terminal, optional
agentctl create --name researcher --agent omp --cwd docs --task "Map the module layout"
agentctl create --name reviewer   --agent claude --cwd . --task "Review src/"

agentctl wait --any               # the next Worker's news
agentctl read researcher
agentctl send researcher "Focus on the parser only"
agentctl list
```

`--workspace` is optional: a command resolves the workspace from the flag, then
`AGENTCTL_WORKSPACE`, then the nearest registered root at or above the current
directory.

## What agentctl does and does not do

| Job | Who does it |
| --- | --- |
| Run a Worker in a terminal that stays alive | Herdr |
| Keep a Worker alive when the Leader exits | Herdr (`--outlive-parent`) |
| Deliver a message | Herdr (`hc send`) |
| Refuse to type into a blocked agent | Herdr |
| Queue the harness's own input | The harness |
| Report whether a send started a turn | Herdr (`started`) |
| Know when a Worker's turn ended | Herdr (`turn` counter, `hc wait`) |
| Notify a parent that a child blocked, errored or exited | Herdr |
| Terminal liveness | Herdr |
| Workspace identity and path migration | agentctl |
| Which terminal is this workspace's Leader | agentctl (a binding, not a lock) |
| Worker names, cwd, agent, worktree | agentctl |
| Native session ids and relaunch on recovery | agentctl |

There is no message queue, no event bus, no daemon and no Worker hook. A Worker
is an ordinary Herdr terminal.

## Commands

| Command | Purpose |
| --- | --- |
| `setup [--default-agent ID] [--force]` | One-time machine setup |
| `init` | Create/load the workspace identity for the current directory (idempotent) |
| `info` | Workspace id, leader id, counts, default agent, runtime status |
| `rebind NEW` | Move this workspace identity to another directory |
| `list [--all] [--no-refresh]` | Sessions of this workspace |
| `create --name N --cwd DIR [--agent ID] [--task TEXT] [--mode shared\|worktree] [--wait S]` | New Worker |
| `delete NAME [--keep-worktree]` | Kill the agent, clean the terminal, retire the record |
| `prune [--dry-run]` | Reclaim agentctl-owned state of soft-deleted sessions |
| `send NAME TEXT [--force] [--wait] [--timeout S]` | Send text through Herdr |
| `keys NAME KEY [KEY...]` | Answer a trust or permission dialog |
| `interrupt NAME [--timeout S]` | Interrupt the agent's current turn |
| `read NAME [--lines N] [--follow]` | Screen plus history |
| `status [NAME] [--no-refresh]` | Session or workspace status |
| `wait NAME [--timeout S]` / `wait --any` / `wait --all` | One turn, or the Workers' next news |
| `recover [NAME]` | Re-discover terminals, or relaunch from a native session id |
| `leader attach\|detach\|status` | Where this workspace's Leader runs |
| `agents list\|inspect ID` | Catalog with measured capabilities |
| `config set\|get\|list` | `default-agent`, `session.default-mode`, `agents.<id>.enabled` |

Every command accepts `--json`.

## Four words worth knowing

- `starting`, `idle`, `working`, `waiting`, `completed`, `failed`, `stopped`,
  `unknown`, `unreachable` — the same words v0.1 used, derived from Herdr's own
  status records. An agent that reports no status is `unknown`, never guessed.
- `started: false` from `send` means the text was typed but no turn began (a
  booting agent, or a dialog). It is a doubt, not a success and not a failure.
- `wait --any` returns on the next Worker's new turn, a block, an error or an
  exit. `wait --all` returns when no Worker is working. Both are `hc wait
  --children`, so Herdr decides.
- A Worker whose terminal is lost is `unreachable`, and `recover` relaunches it
  only from a native session id the catalog says is resumable.

## Two limits stated plainly

1. **A child that ends its turn without reporting does not wake an idle parent.**
   That is Herdr issue [#176], with PR [#177] still open on 0.1.18. A Leader
   inside Herdr therefore hears about `blocked`, `error` and `exit`
   automatically, and hears about a quiet turn end when it calls `wait --any` or
   `wait --all`. agentctl does not paper over this with a hook or a message
   table; if you need to be woken per turn without waiting, follow the upstream
   issue.
2. **The Leader binding is a pointer, not a lock.** Two Leaders are prevented by
   using the same binding, not by a token. `leader status` compares the binding
   with what Herdr says about that terminal, so a stale binding is visible
   instead of expiring silently.

[#176]: https://github.com/herdrdev/herdr-core/issues/176
[#177]: https://github.com/herdrdev/herdr-core/pull/177

## Security boundary

`--cwd` must exist, resolve through symlinks to its real path, and stay inside
the workspace root. Agent commands come from the catalog only. That is a scoping
constraint, not an OS sandbox.

## Docs

- [docs/runtime-capabilities.md](docs/runtime-capabilities.md) — measured Herdr behaviour
- [docs/architecture.md](docs/architecture.md) — modules, data model, delegation
- [docs/cli.md](docs/cli.md) — full CLI reference
- [docs/testing.md](docs/testing.md) — how to run the tests
- [docs/v0.3-report.md](docs/v0.3-report.md) — what this refactor removed and why
