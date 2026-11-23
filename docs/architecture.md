# Architecture

## Layers

```text
cli.py                     parse args, render one result, choose exit code
├── workspace/             identity: canonical paths, UUIDs, info
│   ├── resolver.py        path → realpath → registry lookup/create
│   └── service.py         init / info / rebind
├── session/               lifecycle
│   ├── service.py         create, delete, get, list, recover
│   ├── lifecycle.py       runtime facts → agentctl status
│   ├── messaging.py       send / read / follow / wait
│   └── worktree.py        git worktree per session
├── agents/                catalog
│   ├── definitions.py     measured capabilities per agent id
│   ├── detector.py        is the binary installed, what version
│   └── catalog.py         config + definitions + detection → spec
├── runtime/               terminal substrate
│   ├── base.py            RuntimeAdapter interface
│   └── herdr_core.py      the only module that knows `hc`
├── storage/               persistence
│   ├── database.py        connection, schema, migrations, transactions
│   ├── models.py          dataclasses travelling between layers
│   └── repository.py      SQL for workspaces and sessions
├── config/                config.toml schema, load, save
<!---->
└── errors.py              every failure has a code, hint and exit status
```

Rules kept by the code:

- The CLI never issues SQL and never builds an `hc` command.
- `runtime/` never reads the registry.
- `session/` is the only layer that owns both a `Session` and a
  `RuntimeAdapter`, and it is the only place that maps runtime facts to status.
- `workspace/` knows nothing about agents or terminals.

## Data model

```sql
workspaces(id PK, root_path UNIQUE, leader_id UNIQUE, default_agent,
           created_at, updated_at)

sessions(id PK, workspace_id FK, name, agent, cwd, mode,
         hc_terminal_id, hc_name, native_session_id, native_session_dir,
         worktree_path, worktree_branch, git_root, runtime_started_at,
         status, last_error, created_at, updated_at, deleted_at,
         UNIQUE(workspace_id, name) WHERE deleted_at IS NULL)

agent_catalog(id PK, command_json, enabled, capabilities_json, updated_at)
```

- `workspaces.id` is `ws_<uuid4hex>`; `leader_id` is `leader_<uuid4hex>`.
  Neither is derived from the path: a moved/renamed directory keeps its identity
  until `rebind` says otherwise, and the same path always resolves to the same
  pair after a restart.
- `sessions.id` is `sess_<uuid4hex>` and never equals a terminal address.
- The partial unique index on `(workspace_id, name)` lets a name be reused after
  a session is deleted while still blocking duplicates among live sessions.
- `deleted_at` is a soft delete: history and native ids stay readable.
- WAL + `busy_timeout`; every multi-statement change runs inside
  `BEGIN IMMEDIATE`. `ALTER TABLE` migrations run at open time so an existing
  `registry.db` survives upgrades.

## Session creation

```text
1  resolve workspace (must already exist)          ws_...
2  enforce unique name in this workspace
3  resolve agent:  --agent → workspace default → global default → error
4  validate cwd: realpath, exists, is a dir, inside workspace root
5  mode == worktree ? git worktree add -b agentctl/<name> .agentctl/worktrees/<id>
6  insert the registry row (status=starting)
7  hc run --bg --json --no-parent --name ac-<slug>-<id6> --cwd <cwd> \
        --label agentctl.workspace=... --label agentctl.session=... -- <argv>
8  persist terminal address; capture native session id when the agent writes one
9  anything failing at 5 or 7 rolls the row (and worktree) back
```

`--no-parent` is not cosmetic: measured on hc 0.1.18, a terminal started from
inside another HC terminal inherits `die_with_parent=true`. Without the flag
every worker would die when the Leader's own terminal closes.

Agent argv is built from the catalog entry plus the measured session-id flags:

| agent | argv today |
| --- | --- |
| omp | `omp --session-dir ~/.config/agentctl/sessions/<sess>` |
| pi | `pi --session-id <uuid>`, `--session-dir` private dir |
| claude | `claude --session-id <uuid>` |
| codex | `codex` (no launch-time id pin) |

## Status derivation

`lifecycle.derive_status` is pure: session + terminal + integration flag →
`(status, reason)`. It is the only place a status word is produced.

| runtime says | agentctl |
| --- | --- |
| terminal missing | `unknown` ("runtime terminal not found") |
| finished, reason `killed` | `stopped` |
| finished, exit ≠ 0 | `failed` ("agent exited with code N") |
| finished, exit 0 | `stopped` |
| live, `idle` | `idle` |
| live, `working` | `working` |
| live, `done` | `completed` |
| live, `blocked` | `waiting` (needs a person) |
| live, `error` | `failed` |
| live, no word, agent reports status | `starting` within the grace window |
| live, no word, agent does not report | `unknown` |

The grace window (90 s) is measured from `runtime_started_at`, which every
relaunch resets, so a resumed session is not immediately judged broken.

## Recovery

```text
recover(session):
  terminal = get(hc_terminal_id)                    ← persisted address
          or ls --label agentctl.session=<id>       ← address lost / new core
  if terminal live            → attach: persist address, refresh status
  elif native_session_id and agent supports resume
                              → hc run --resume <native>, persist new address
  else                        → status=unreachable, row kept
```

A failed recovery never deletes the row. Deletion is always an explicit user
action, and even then the row is soft-deleted.

## Messaging

- `send` validates the session is this workspace's, resolves the terminal
  through the registry/labels, waits until the agent has reported once (a send
  into a booting agent can be dropped), then passes the text as a single argv
  element so quotes, `$VAR`, leading `-` and embedded newlines arrive verbatim.
- `read` reports `complete`/`why` from HC so partial history (agent busy,
  full-screen app) is labelled rather than silently truncated.
- `wait` is HC's own wait: it returns on a status change, on exit, or on a
  timeout check-in (`ended: false`), and never fabricates completion. For agents
  that do not report status (codex) it waits for the process to exit.

## Worktree policy

- Worktrees live in `<workspace>/.agentctl/worktrees/<session id>` and are
  excluded through `.git/info/exclude`, so `git status` in the main checkout
  stays clean.
- Deletion removes a worktree only when it is clean; uncommitted worker output
  is project data, so the worktree is kept and the reason is recorded in the
  session's `last_error`.
- `git branch -d` (never `-D`) drops the branch: merged work disappears, worker
  commits survive.
