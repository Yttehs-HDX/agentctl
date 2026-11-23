# agentctl

Workspace-scoped agent session manager on top of [Herdr Core](https://github.com/) (`hc`).

`agentctl` gives a Leader agent one CLI to own a pool of independent worker sessions:
create them, send them work, read their output, wait for them, and get the same pool
back after a restart.

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

agentctl --workspace="$(pwd)" init
agentctl --workspace="$(pwd)" create --name researcher --cwd "$(pwd)/docs"
agentctl --workspace="$(pwd)" send researcher "Map the module layout"
agentctl --workspace="$(pwd)" wait researcher
agentctl --workspace="$(pwd)" read researcher
agentctl --workspace="$(pwd)" list
```

After the Leader exits and comes back, the same commands find the same workspace,
the same sessions and the same terminals.

## Commands

| Command | Purpose |
| --- | --- |
| `setup [--default-agent ID] [--force]` | One-time machine setup |
| `init` | Create/load the workspace identity for `--workspace` (idempotent) |
| `info` | Workspace id, leader id, counts, default agent, runtime status |
| `rebind NEW` | Move this workspace identity to another directory |
| `list [--all]` | Sessions of this workspace |
| `create --name N --cwd DIR [--agent ID] [--mode shared\|worktree] [--base-ref REF]` | New worker session |
| `delete NAME [--keep-worktree]` | Kill the agent, clean the terminal, retire the record |
| `send NAME TEXT` / `send NAME -` | Send a message (stdin keeps bytes intact) |
| `read NAME [--lines N] [--follow]` | Screen + history |
| `status [NAME]` | Session or workspace status |
| `wait NAME [--timeout S]` | Wait until the agent stops working |
| `recover [NAME]` | Re-discover terminals, or relaunch from a native session id |
| `agents list\|inspect ID` | Catalog with measured capabilities |
| `config set\|get\|list` | `default-agent`, `session.default-mode`, `agents.<id>.enabled` |

Every command accepts `--json`. `--workspace` is mandatory for workspace and session
commands; it is never inferred from the current directory.

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
- [docs/architecture.md](docs/architecture.md) — modules, data model, recovery
- [docs/cli.md](docs/cli.md) — full CLI reference
- [docs/testing.md](docs/testing.md) — how to run the tests
