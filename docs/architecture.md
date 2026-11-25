# Architecture

## Layers

```text
cli.py                     parse args, resolve the workspace, render one result
├── workspace/             identity: canonical paths, UUIDs, discovery
│   ├── discovery.py       flag > env > cwd > nearest registered parent
│   ├── resolver.py        path -> realpath -> registry lookup/create
│   └── service.py         init / info / rebind
├── leader/                the binding: which terminal leads this workspace
├── session/               lifecycle
│   ├── service.py         create, delete, get, list, recover, prune
│   ├── lifecycle.py       runtime facts -> agentctl status
│   ├── messaging.py       send / keys / interrupt / read / waits
│   └── worktree.py        git worktree per session
├── agents/                catalog: declarative launch and resume flags
│   ├── definitions.py     measured capabilities per agent id
│   ├── detector.py        is the binary installed, what version
│   └── catalog.py         config + definitions + detection -> spec
├── runtime/               terminal substrate
│   ├── base.py            RuntimeAdapter interface
│   └── herdr_core.py      the only module that knows `hc`
├── storage/               persistence
│   ├── database.py        connection, schema, migrations, transactions
│   ├── models.py          dataclasses travelling between layers
│   └── repository.py      SQL for workspaces, sessions, the leader binding
├── config/                config.toml schema, load, save
└── errors.py              every failure has a code, hint and exit status
```

Four modules sit outside those packages: `paths.py` resolves the config and
state directories, `skills.py` installs the bundled Leader skill, `clock.py`
holds the one timestamp format stored rows use, and `__init__.py` carries the
version.

Rules kept by the code:

- The CLI never issues SQL and never builds an `hc` command.
- `runtime/` never reads the registry.
- `session/` is the only layer that owns both a `Session` and a
  `RuntimeAdapter`, and the only place that maps runtime facts to status.
- `workspace/` knows nothing about agents or terminals.
- `leader/` knows about one terminal and nothing about Workers.

## Delegation to Herdr

The guiding rule: if Herdr already does it, agentctl calls Herdr. The wrapper
only adds what Herdr has no concept of.

| Concern | Owner | Where |
| --- | --- | --- |
| Terminal lifecycle, persistence, recovery of lost programs | Herdr | `hc run`, `hc keep` |
| Worker survival after the Leader ends | Herdr | `--outlive-parent` |
| Parent/child relation and native child notes | Herdr | `--parent` |
| Message delivery, blocked-agent guard, readiness | Herdr | `hc send`, `--force`, `--wait` |
| Turn counting and completion | Herdr | OSC 7501 `turn`, `hc wait`, `hc wait --children` |
| Status words | Herdr | OSC 7501 `status` |
| Workspace identity, path migration | agentctl | `workspace/` |
| Which terminal is the Leader | agentctl | `leader/` |
| Worker names, agents, worktrees, native ids | agentctl | `session/` |

## Leader binding

A row in `leader_bindings`: `workspace_id → terminal, native_session_id`. No
token, no epoch, no expiry.

- `leader attach` records the terminal (`$HC_TERMINAL_ID` by default), labels it
  `agentctl.role=leader`, and refuses a terminal already bound to another
  workspace, or one carrying `agentctl.role=worker`. The staleness check, the
  row write and the label share one `BEGIN IMMEDIATE` transaction, so a binding
  is never visible without its label and two attaches cannot both win.
- `leader status` asks Herdr whether that terminal is live. A finished terminal
  means the binding is stale, so any Leader may attach; nothing has to expire.
- `leader detach` forgets the row. The terminal keeps running.

v0.2 had a lease with expiring tokens and epoch fencing. It was removed because
it re-implemented a supervisor: Herdr already knows whether a terminal is alive,
and the lease's failure modes (renewals, epochs, "who holds it now") were cost
without a guarantee an OS boundary could back.

## Workers are Herdr children

```text
agentctl create --name w1 --cwd . --task "do X"
  └─ hc run --bg --name ac-w1-<id6> --cwd . \
        --parent <leader terminal> --outlive-parent \
        --label agentctl.workspace=<ws> --label agentctl.session=<sess> \
        --label agentctl.role=worker -- <agent argv> "do X"
```

- `--parent` gives the Worker Herdr's native notes (blocked, error, exit) into
  the Leader's terminal.
- `--outlive-parent` keeps the Worker alive when the Leader's program ends.
- `--task` appends the first message at launch, so a booting agent never drops
  it.
- The labels are the durable link: a restarted Leader finds its Workers by
  `agentctl.workspace=<ws>`, independent of terminal addresses.

With no Leader bound the Worker is parentless: it loses the notes but never its
survival.

## Waits

```text
agentctl wait NAME      → hc wait <terminal>          one session's turn
agentctl wait --any     → hc wait --children any      news from the bound Leader's children
agentctl wait --all     → hc wait --children all      until those children stop working
```

Group waits require a live bound Leader. Taking over does not reparent the
old Leader's Workers. Wait for them by name.

`WaitOutcome.turn_ended` is `reason == "status"`: Herdr saw the child leave
`working`. That is the agent-agnostic completion signal, verified on 0.1.18 with
a scripted harness that reported `working → done` and never sent anything.

`reason: timeout` is a check-in, never a failure.

## Known upstream limit

A child that ends its turn without `hc send` does not wake an idle parent.
Herdr types notes for blocked, error and exit only; issue #176 asks for the turn
end too, and PR #177 is open but not merged on 0.1.18. A Leader therefore learns
about a quiet turn end by calling `wait --any`, and nothing in agentctl invents a
second notification path to hide that.

## Data model

```sql
workspaces(id PK, root_path UNIQUE, leader_id UNIQUE, default_agent,
           created_at, updated_at)

sessions(id PK, workspace_id FK, name, agent, cwd, mode,
         hc_terminal_id, hc_name, native_session_id, native_session_dir,
         worktree_path, worktree_branch, git_root, runtime_started_at,
         status, last_error, created_at, updated_at, deleted_at,
         UNIQUE(workspace_id, name) WHERE deleted_at IS NULL)

leader_bindings(workspace_id PK FK, leader_id, terminal, native_session_id,
                attached_at, updated_at)
```

- Only agentctl's own facts live here. Herdr owns terminal state, status and
  turn counts; they are queried, never mirrored.
- `workspaces.id` is `ws_<uuid4hex>` and `leader_id` is `leader_<uuid4hex>`.
  Neither is derived from the path, so a moved directory keeps its identity
  until `rebind` says otherwise. `rebind` rewrites every stored path under the
  old root (`cwd`, `worktree_path`, `git_root`, `native_session_dir`) in one
  transaction, then repairs moved worktrees with `git worktree repair <path>`.
  Lookup is `root_path` first, then filesystem identity `(st_dev, st_ino)`, so a
  differently-cased spelling of a registered root (`/tmp/CaseX` for
  `/tmp/casex`) resolves to that workspace instead of creating a second one; the
  same identity rule backs `is_inside`, workspace discovery and stored session
  paths, which are respelled under the registered root.
- `sessions.id` is `sess_<uuid4hex>`, never equal to a terminal address.
- `deleted_at` is a soft delete: history and native ids stay readable.
- Schema version 4 drops the v0.2 tables (`messages`, `events`, `inbox`,
  `outbox`, `consumer_offsets`, `integrations`, `leader_leases`). Workspaces and
  sessions are migrated in place, so an upgraded registry keeps its identity and
  its Worker list.

## Session creation

```text
1  resolve workspace (must already exist)          ws_...
2  enforce unique name in this workspace
3  resolve agent:  --agent → workspace default → global default → error
4  validate cwd: realpath, exists, is a dir, inside workspace root
5  mode == worktree ? git worktree add -b agentctl/<slug>-<id8> .agentctl/worktrees/<id>
6  insert the registry row (status=starting)
7  hc run --bg --json --name ac-<slug>-<id6> --cwd <cwd> \
        --parent <leader terminal> --outlive-parent \
        --label agentctl.workspace=... --label agentctl.session=... \
        --label agentctl.role=worker -- <argv> [first task]
8  persist terminal address; capture native session id when the agent writes one
9  anything failing at 5 or 7 rolls the row (and worktree) back
```

A Worker is deliberately a child of the Leader's terminal *and* allowed to
outlive it. Measured on hc 0.1.18: `--parent` alone sets `die_with_parent=true`,
which would kill every Worker when the Leader's terminal closes;
`--outlive-parent` keeps `parent=<leader>` while `die_with_parent` becomes false.
The relation therefore survives for Herdr's native notes, and the Worker
survives the Leader.

Each agent's argv comes from its catalog entry and the measured session-id
flags it needs:

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

## Addresses: local vs remote

`hc` addresses a terminal it manages on another machine as `machine/t7`
(`hc run --on`). agentctl v0.1 only creates local terminals, so:

- `TerminalInfo.is_local` is false for any address containing `/`;
- terminal discovery (`find_terminal`, and therefore `send`, `read`, `delete`,
  `recover`) ignores remote terminals — otherwise a remote terminal that
  happens to carry the same `agentctl.session` label would be adopted: `send`
  would type on another machine, `delete` would kill work agentctl never
  started, and `recover` would silently rebind the session to it;
- `info` reports only local terminals as orphans for the same reason.

`hc ls` returns this machine's terminals first, then each connected machine's,
so filtering by address is the only reliable discriminator.

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

- `send` resolves the session's terminal and calls `hc send`. Text travels as
  one argv element, so quotes, `$VAR`, a leading `-` and embedded newlines
  arrive verbatim.
- Herdr's guard is kept: a blocked agent is refused unless `--force`, and a
  finished terminal is an error, not a silent no-op.
- `--wait` passes to `hc send --wait`, which returns the turn it started.
  `started: false` in the result means no turn began; agentctl reports that
  rather than claiming delivery.
- `keys` and `interrupt` expose `hc keys` and `hc interrupt`. Key names starting
  with `-` are rejected at the service boundary.
- `read` reports `complete`/`why` from Herdr. A full-screen program returns
  `complete: false` with the screen alone, so partial history is labelled rather
  than silently truncated.

## Worktree policy

- Worktrees live in `<workspace>/.agentctl/worktrees/<session id>` and are
  excluded through `.git/info/exclude`, so `git status` in the main checkout
  stays clean.
- Branch names are `agentctl/<slug>-<first 8 hex of the session id>`. The slug
  is reduced to characters git accepts (`..`, `.lock`, `@{`, leading dots and
  `~^:?*[\` are normalised away) and the id suffix keeps names unique per
  repository, so a reused session name never collides with a branch left
  behind by a kept worktree. The final ref is checked with
  `git check-ref-format` before use.
- Deletion removes a worktree only when it is clean; uncommitted Worker output
  is project data, so the worktree is kept and the reason is recorded in the
  session's `last_error`.
- `git branch -d` (never `-D`) drops the branch: merged work disappears, Worker
  commits survive.
