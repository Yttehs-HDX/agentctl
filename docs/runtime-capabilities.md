# Herdr Core runtime capabilities

Measured on this machine against **hc 0.1.18** (installed by nix, core
`shetty-laptop`). Everything below was executed in a scratch core
(`HC_SESSION=lab`, dedicated `XDG_*`), not against the user's live terminals.
Anything not reproduced is marked **[UNVERIFIED]**.

## 1. Command surface

```text
hc run --bg --json --no-parent --name <n> --cwd <dir> --label k=v -- <argv...>
hc get --json <t>            # facts: state, status{}, cwd, exit_code, reason, labels
hc ls --json [--all] [--label k=v]
hc read --json --lines N <t> # {text, complete, how, why}
hc send --json [--force] <t> <text>
hc wait --json [--timeout S] <t>
hc kill --json <t> ; hc rm --json <t>
hc server status --json ; hc connect (JSON-RPC over stdio) ; hc schema
```

`hc --json` is the default when stdout is a pipe, but agentctl passes it
explicitly so a TTY never changes the shape.

## 2. Terminal creation

`hc run --bg ...` returned in ~30 ms with
`{"address":"t1","agent":"sleep","cwd":"/tmp/hclab/work/sub","name":"probe","state":"live","turn":0}`.

- **Explicit CWD works** and is reported back verbatim by `get` (`cwd`) and by
  `foreground.cwd` (resolved by the OS).
- **Duplicate live names are rejected**: `{"kind":"name_taken"}`, exit 1.
- **Name charset**: `1-32 of a-z 0-9 - _ . starting with a letter or digit, not
  shaped like an id`. `Bad.Name` → `invalid_params` (exit 6); `a_b` accepted.
  Reuse after `kill` works (finished terminals do not hold the name).
- **Spawn failure is reported synchronously**, not as a live terminal:
  bad cwd or missing binary → `{"kind":"spawn_failed", ...}` on stderr, exit 1,
  and `hc get <name>` → `not_found`. No rollback is needed for that case.
- **Labels**: `--label k=v` (repeatable) rides along and is queryable via
  `hc ls --label k=v` and `get.labels`. Values may contain `-` `_` `.` and
  uppercase. agentctl labels every terminal `agentctl.workspace` and
  `agentctl.session` — this is how a session's terminal is rediscovered after a
  leader restart, independent of the address.

### Parent semantics (important)

Without `--no-parent`, a terminal started from inside another HC terminal gets
`parent=<caller>` and `die_with_parent=true`; killing the parent kills it:

```text
A(parent=t1h, die_with_parent=true) → killed when t1h was killed
B(--no-parent)                     → stayed live
```

Because the Leader often runs inside an HC terminal, **agentctl always passes
`--no-parent`**. Otherwise every worker would die with the Leader's terminal,
violating the product requirement that workers outlive the Leader.

## 3. Talking to a terminal

`hc send <t> <text>` pastes and submits. Verified:

- The text is passed **verbatim**: leading `-` (`-- "-x --if=idle"`), single and
  double quotes, `$VAR`, and embedded newlines all arrived byte-identical at a
  `read`-loop reader. Multi-line text becomes one paste; omp received it as one
  message ("Received 2 lines").
- It refuses a blocked agent: `{"kind":"blocked"}`, exit 9, unless `--force`.
- It refuses a finished terminal: `{"kind":"finished"}`, exit 11.
- `SendResult` = `{started, status, turn}`; `started` is null for programs that
  do not report status.
- A send issued ~100 ms after `run --bg` of omp arrived correctly
  (`started: true`, turn advanced) — but the hc manual warns a send to a booting
  agent may be dropped, so agentctl waits for the first status report
  (`reported: true` or first status word) before sending.

`hc read --json` = `{text, complete, how, why}`.

- `how` is `screen`, `history`, or `stitched`.
- When the app is `working`/`blocked`/`error`, history reads return only the
  screen and set `complete: false` with `why` = that status. This is why
  `agentctl read` distinguishes "partial" output.
- Reading a finished terminal works (`how: "history"`).

`hc wait --json <t>` returns
`{terminal, ended, reason, status, exit_code, turn, children, ...}`.

- `reason: status` when the agent stopped working, `timeout` for a check-in
  (`ended: false`), `exit` when the program died.
- A program that never reports status is "working until it exits", so
  `wait` on codex blocks until the process exits — not usable as a turn barrier.

## 4. Status reporting (OSC 7501)

`hc get` exposes `status: {app, state, kind, msg, progress}` and `turn`.
Measured:

| agent | reports status | startup state | notes |
| --- | --- | --- | --- |
| omp 19.1.0 | yes (`app: omp`) | `idle` within ~6 s | `idle → working → done` per turn; `turn` increments |
| pi | yes (`app: pi`) | `idle` within ~8 s | same vocabulary |
| claude 2.1.327 | yes (`app: claude-code`) | `blocked` with `kind: permission`, `msg: "trust this folder"` on a fresh directory | after `down enter` (trust) → `idle` |
| codex 0.146.0 | **no** (`reported: false`, `status: null`) | — | agentctl shows `starting` for 90 s, then `unknown`; `wait` cannot be used as a turn barrier |
| opencode | not installed | — | [UNVERIFIED] |

This is why agentctl stores `integration` per agent and maps "no status" to
`unknown`, never to `idle`/`working`. Note `blocked` maps to agentctl `waiting`
and is a human decision point (send refuses without `--force`).

## 5. Native session ids and resume

| agent | how agentctl pins/learns the id | resume |
| --- | --- | --- |
| omp | `omp --session-dir <private dir>`; the CLI writes `<dir>/<ts>_<uuid>.jsonl` and the uuid is the session id | `omp --session-dir <dir> --resume <uuid>` — verified: restored the earlier turn's context |
| pi | `pi --session-id <uuid>` creates the session under that id (verified: file named `<ts>_<uuid>.jsonl`) | `pi --session <id>` — verified, restored context. **`pi --resume` without an id only opens a picker and restores nothing** (observed: empty context, "No sessions in current folder") |
| claude | `claude --session-id <uuid>` stores `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` with that exact id | `claude --resume <uuid>` — verified: after a terminal kill+rm, recovered agent answered the pre-kill code word correctly |
| codex | no launch-time id pin; rollouts are `~/.codex/sessions/<date>/rollout-<ts>-<uuid>.jsonl` | `codex resume <id>` exists, but binding a *new* terminal to a known id at launch was **[UNVERIFIED]** without a picker. A send to a codex TUI is delivered and echoed, but the TUI may then fail on its own model config (observed: `model_not_found` 400) — not an agentctl send failure |

Consequence for v0.1: codex sessions get `native_session_id = NULL` and
`supports_resume = false` in the catalog, and their status is `unknown`.
omp/pi/claude get private session directories under
`~/.config/agentctl/sessions/<agentctl session id>/` so agentctl never has to
guess which file belongs to which worker.

Caveat observed: resuming an omp session while the original process still holds
the file makes omp fork to a new file. Recovery therefore only resumes when the
original terminal is gone, or forks deliberately.

## 6. Persistence and recovery

- `hc server restart` kept 5 live terminals alive across the core restart.
  `get`, `ls`, `read` all still worked afterwards, including status words.
- Terminal addresses (`t3`) are stable for the life of the core.
- Finished terminals stay queryable (`get`, `read`) until removed with `hc rm`;
  `rm` on a live terminal fails with `{"kind":"live"}`, so agentctl kills first.
- A session's terminal can be found **without** the address via
  `hc ls --label agentctl.session=<id>` — the recovery path used after a Leader
  restart or a lost registry value.
- `hc rm` on a finished terminal also drops it from `get`, so agentctl never
  deletes the registry row when the runtime forgets a terminal: it marks the
  session `unreachable` instead.

## 7. Server and environment

- `hc ls` autostarts the core if none runs (no error). `hc server status --json`
  gives `{core:{id,name}, pid, version, live, finished, holders}`.
- `hc` inherits the caller's environment. `HC_TERMINAL_ID` must be stripped when
  spawning, otherwise the caller's terminal becomes the parent.
- hc has **no flag to inject environment variables** into a new terminal —
  agentctl passes per-session configuration through argv only.

## 8. Error envelope and exit codes

`stderr` carries `{"error": {"kind", "message", "hint"}}`. Observed kinds:
`not_found` (3), `needs_target` (7), `blocked` (9), `status_mismatch` (10),
`finished` (11), `name_taken` (1), `spawn_failed` (1), `invalid_params` (6),
`live` (1). agentctl maps these onto its own error classes while preserving the
original word as `runtime_kind`.

## 9. JSON-RPC (`hc connect`)

`hc connect` speaks JSON-RPC 2.0 over stdio; multiple requests can be written to
one process (`terminal.get`, then `terminal.list`, each answered by `id`).
Methods of interest: `terminal.run/get/list/read/send/wait/kill/rm/label`,
`server.status`, `events.subscribe`. v0.1 uses the CLI (simpler failure modes,
same JSON shapes); the RPC surface is the documented upgrade path.

## 10. Measurements added later

- **pi resume**: `pi --session <id>` reloaded a real session (context counter
  went to 15k, the agent recalled a pre-kill code word). `pi --resume` with no
  id opened the session picker with an empty context — the catalog now uses
  `--session`.
- **claude resume**: after `hc kill` + `hc rm`, `claude --resume <uuid>` in a
  fresh terminal restored the conversation; the agent answered the pre-kill
  code word correctly.
- **codex send**: input is delivered and echoed in the TUI transcript, but the
  TUI itself then failed with `model_not_found` (400) for the configured model
  on this account. That is the agent's own configuration, not a lost message.
- **concurrency**: two simultaneous `create` calls for the same name produced
  exactly one session and one clear conflict; eight parallel creates all
  landed and `PRAGMA integrity_check` returned `ok` with `journal_mode=wal`.
- **orphan detection**: a terminal labelled `agentctl.workspace=<id>` without a
  registry row shows up under `info` → `orphans`.
