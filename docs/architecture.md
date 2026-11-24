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

## v0.2: event-driven orchestration

v0.1 answered "what is this session doing right now". v0.2 answers "what happened
while I was busy", which needs three things v0.1 did not have: an identity for
the Leader that can be lost, a durable record of each dispatched message, and a
process that listens when no CLI command is running.

```text
cli.py
├── workspace/discovery.py     flag > env > cwd > nearest registered parent
├── leader/service.py          lease, epoch, token, fencing
├── messaging/
│   ├── transitions.py         the legal state machine, testable without SQL
│   └── service.py             enqueue, dispatch, ACK, complete, honest unknowns
├── events/
│   ├── bus.py                 append-only log + Leader inbox, dedupe by key
│   └── ingest.py              Herdr Core terminal facts → agentctl events
├── spool.py                   hook observations → registry (atomic claim)
├── integrations/              per-harness hook assets and capabilities
├── daemon/service.py          agentctld: stream, lease, dispatch, delivery
└── diagnostics.py             doctor
```

Rules kept by the new code:

- `messaging/transitions.py` owns the state vocabulary; no service invents one.
- Only `integrations/` knows a harness's event names.
- Only `events/ingest.py` maps runtime facts to events, and it is pure.
- The daemon composes services; it holds no state the database does not.

## Leader lease

`leader_leases` has one row per workspace, so SQLite's own write serialization
arbitrates a claim. The row carries `leader_instance_id`, `leader_epoch`,
`lease_token` and `expires_at`.

```text
attach   no live lease, or the same instance  → claim / renew, epoch unchanged
attach   a live lease held by someone else     → LeaderConflict
takeover forced                                → epoch + 1, new token
expire   renewed_at + lease_seconds passed     → claimable again
```

Fencing is `authorize()`: once a lease exists, a mutating command must present
its token, and an old token fails because the stored epoch moved on. Before any
lease exists the workspace is unclaimed, so single-operator use is unchanged.

## Message lifecycle

```text
queued ─dispatch─▶ dispatching ─accepted─▶ dispatched ─ACK─▶ acknowledged
   ▲                    │                      │                  │
   └──refused───────────┘                      │                  ▼
                                               │               running
                                               ▼                  │
                                        delivery_unknown          ▼
                                                          completed / failed
```

- The row is written **before** the text is sent, and `dispatching` is committed
  before the transport call. A crash in that window leaves a row the sweep turns
  into `delivery_unknown`, never into a silent retry.
- One message is in flight per session (a partial unique index); queued messages
  wait. That is what makes per-worker ordering real, while workers stay parallel.
- An ACK names a message id. The hook reads it out of the text it received, so
  the ACK proves *that* message arrived, not merely that the worker was idle.
- `retry` refuses `delivery_unknown` on purpose: the text may already be in the
  worker's context.

## Hooks report through a spool, not a subprocess

Measured on OMP 19.1.0: `pi.exec` cannot spawn from an `input` handler
(`ENOENT ... posix_spawn`), and an extension that declares no flag is loaded only
after the first prompt. So the generated hook:

- registers a flag, forcing an eager load;
- appends one JSON line per observation to `$AGENTCTL_HOME/spool/<agent>.jsonl`
  (`node:fs` works from every handler);
- never throws into the host.

Any CLI command and the daemon drain that spool. A drain `rename`s the file
before reading it, so one reader owns each record and a concurrent append always
lands in a fresh file. Ingest re-checks workspace and session, so a record can
never mark another session's message delivered.

## Event model

```text
{"schema_version": 1, "event_id": "evt_…", "event": "turn.completed",
 "workspace_id": "ws_…", "session_id": "sess_…", "message_id": "msg_…",
 "leader_epoch": 1, "source": "omp", "severity": "info",
 "timestamp": "2026-11-25T00:00:00+00:00", "payload": {}, "dedupe_key": "…"}
```

`turn.completed` means the worker's conversation turn ended. It does **not**
mean the task succeeded; the result text is in the payload and the judgement
stays with the Leader.

Leader-relevant events also get an `inbox` row. Delivery is a harness wake where
one exists (`Stop` hook returning `decision: "block"`, or
`sendMessage({triggerTurn: true})` at settle); otherwise the row simply waits.
Dedupe is `(workspace, event, dedupe_key)`, so a replayed hook is a no-op.

## agentctld

One supervised process per workspace, started with `agentctl daemon start`:

```text
loop:  ingest spool → dispatch queued → run outbox → deliver inbox → sleep 1 s
bg:    hc events --json --label agentctl.workspace=<id>, reconnecting on loss
       heartbeat the lease every lease_seconds / 3
```

A restart re-reads `outbox`/`inbox`, so crash recovery is "the work is still
pending", not a replay of side effects. A lost lease stops dispatch: a fenced
Leader must not act.

## Data model

```sql
workspaces(id PK, root_path UNIQUE, leader_id UNIQUE, default_agent,
           created_at, updated_at)

sessions(id PK, workspace_id FK, name, agent, cwd, mode,
         hc_terminal_id, hc_name, native_session_id, native_session_dir,
         worktree_path, worktree_branch, git_root, runtime_started_at,
         status, last_error, created_at, updated_at, deleted_at,
         UNIQUE(workspace_id, name) WHERE deleted_at IS NULL)
```

The agent catalog is recomputed from config, measured definitions and local
detection on every call; it is not persisted.

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
5  mode == worktree ? git worktree add -b agentctl/<slug>-<id8> .agentctl/worktrees/<id>
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

- `send` requires proof of delivery: it resolves the terminal, rejects agents
  whose catalog definition has `integration = false` immediately, waits up to
  15 s for a status report, and fails with `DeliveryUnconfirmed` (exit 8) if the
  agent never reports. `--force` is the explicit opt-out and reports
  `confirmed: false`. Text is passed as a single argv element so quotes, `$VAR`,
  leading `-` and embedded newlines arrive verbatim.
- `keys` and `interrupt` expose Herdr Core's `hc keys` and `hc interrupt` for
  trust/permission dialogs and cancellation. Both resolve the live terminal
  without a readiness wait; key names starting with `-` are rejected at the
  service boundary.
- `prune` reclaims state agentctl owns for this workspace's soft-deleted rows:
  it removes `native_session_dir` trees only when they are direct children of
  `session_state_dir()`, then hard-deletes the row. Worktrees and branches are
  never touched; agent-global native session stores are out of scope.
- `read` reports `complete`/`why` from HC so partial history (agent busy,
  full-screen app) is labelled rather than silently truncated.
- `wait` is HC's own wait: it returns on a status change, on exit, or on a
  timeout check-in (`ended: false`), and never fabricates completion. For agents
  that do not report status (codex) it waits for the process to exit.

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
- Deletion removes a worktree only when it is clean; uncommitted worker output
  is project data, so the worktree is kept and the reason is recorded in the
  session's `last_error`.
- `git branch -d` (never `-D`) drops the branch: merged work disappears, worker
  commits survive.
