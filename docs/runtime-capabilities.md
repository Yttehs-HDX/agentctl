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

Keys and interrupts (measured on hc 0.1.22 against a scratch core):

```text
hc keys --json <terminal> <key>...        -> {"status": "idle"|null, "turn": 0}
hc interrupt --json [--timeout S] <term>  -> {"key": "ctrl+c", "outcome": "exited"}
```

Key names: `enter`, `esc`, `tab`, `up`, `down`, `left`, `right`, `ctrl+c`,
`alt+x`, `f5`, `a`. A key name never starts with `-`; agentctl rejects those at
the service boundary so `hc` never parses one as an option. Both calls resolve
the terminal directly (no readiness wait); a finished terminal raises
`RuntimeFinished` through the same error mapping as `send`.

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

Codex is **explicitly out of scope** for v0.1 (decision 2026-11-24): its trust
and login dialogs swallow typed text while `hc send` still reports success, so
any send-based workflow needs a delivery proof the agent does not provide. The
catalog keeps `session_id="none"`, `supports_resume=false`; a codex session is
`unknown` and unrecoverable by design rather than silently wrong. Findings kept
for a later version:

- a fresh `CODEX_HOME` starts a login flow, not a usable agent;
- `hc` starts programs with the *core's* environment, so `CODEX_HOME=<private>`
  set on the `hc` process never reaches the program — `env K=V cmd` in argv does;
- rollouts (`~/.codex/sessions/<date>/rollout-<ts>-<uuid>.jsonl`) appear only
  after the first real message and carry `session_meta.session_id`, which
  `codex resume <id>` accepts and was measured to restore context.
| opencode | not installed | — | [UNVERIFIED] |

This is why agentctl stores `integration` per agent and maps "no status" to
`unknown`, never to `idle`/`working`. Each catalog entry also records the
release its capabilities were measured on; `agents list`, `agents inspect` and
`setup` report a mismatch as unverified instead of pretending it still holds. Note `blocked` maps to agentctl `waiting`
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

## 10. Remote terminals (`--on`)

`hc run --on <machine>` returns `machine/t7`, and `hc ls` mixes those into the
same `terminals` array after the local ones. Measured with a second core on the
same box (`hc machine add --via 'env HC_SESSION=other bash -c' --name boxb`):

- verified: creation, `get`, `ls`, labels, `kill` all work with the prefixed
  address;
- a remote terminal carrying the same `agentctl.session` label sorts *after*
  the local one, so index-based lookup is safe, but a lost local terminal makes
  the remote one the only match — agentctl therefore rejects any address that
  contains `/` instead of relying on order;
- orphan detection must compare addresses as opaque strings; a bare-address
  comparison would report `boxb/t2` as the local `t2`.

agentctl v0.1 has no `--on` flag: `create_terminal` never passes it.

## 11. Measurements added later

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

---

# v0.2 capability matrix

Every row below was executed on this machine. A cell no measurement backs says so
instead of guessing. Measured on **Herdr Core 0.1.18, OMP 19.1.0, Pi 1.5.2,
Claude Code 2.1.327**, 2026-11-25.

## What each harness can do

| Capability | omp | pi | claude | codex | opencode |
| --- | --- | --- | --- | --- | --- |
| Reports HC status words | yes | yes | yes | **no** | unknown |
| Hook receives message text | yes (`input`) | yes (`input`) | yes (`UserPromptSubmit`) | **no** | unverified |
| Exact per-message ACK | yes | yes | yes | **no** | unverified |
| Turn start event | yes | yes | **no** | no | unverified |
| Turn end event | yes (`agent_end`) | yes (`agent_end`) | yes (`Stop`) | no | unverified |
| Idle wake without typing | yes (`sendMessage`) | yes (`sendMessage`) | yes (`Stop` block) | **no** | unverified |
| Verified on this machine | yes | yes | yes | n/a | no |

Codex and OpenCode are deliberately not integrated: codex emits no Herdr Core
status record and has no verified hook, so a send to it can only be
`delivery_unknown` and its completion rests on a status stream it does not
produce.

## Herdr Core event stream

`hc events --json` streams one JSON document per line:

```text
{"channel":null,"seq":4,"terminals":[...]}                       snapshot, first line
{"jsonrpc":"2.0","method":"event","params":{"seq":8,"terminal":"t2",
 "type":"status","data":{"reported":true,"status":{"app":"omp","state":"idle"}}}}
{"jsonrpc":"2.0","method":"event","params":{"seq":53,"terminal":"t2",
 "type":"turn","data":{"state":"done","turn":1}}}
{"seq":9}                                                         heartbeat
```

Measured event types: `created`, `foreground`, `title`, `status`, `turn`,
`finished`, `labels`, `removed`. `--label k=v` filters. `events.subscribe`
(JSON-RPC) returns `{seq, terminals}` plus the same notifications, and sends a
fresh snapshot to a subscriber that fell behind.

`status: working → done` plus a `turn` bump is an agent-agnostic
`turn.completed`; agentctl uses it as the fallback path for every agent that
reports status. The mapper is `events/ingest.py` and dedupes on `(session, turn)`
so a replayed pair, or a resumed core replaying its snapshot, yields one event.

## OMP 19.1.0 — measured extension facts

- `pi.on("input", ...)` fires for `hc send` text with
  `{type:"input", text, source:"interactive", images}`.
- `turn_start`, `turn_end` (`{turnIndex, message}`), `agent_start`, `agent_end`,
  `session_stop`, `message_end`, `tool_call`, `tool_result` all fire with the
  documented payloads.
- **`agent_settled` did not fire in the TUI** on 19.1.0, although `agent_end`
  did, once per run. Pi 1.5.2 fires both. The hook therefore reports completion
  from `agent_end`, the event both harnesses emit.
- `ctx.mode` is `"tui"` inside an `hc` terminal, `"print"` for `-p`.
- **`pi.exec` cannot spawn from an `input` handler**: it rejects with
  `ENOENT ... posix_spawn`. This is why the hook reports by appending a JSON line
  to a spool file (`node:fs` works from every handler) instead of calling a
  command.
- **An extension that declares no flag and no shortcut is loaded only after the
  first prompt**, so its `input` handler misses exactly that message.
  `pi.registerFlag(...)` forces an eager load; the generated hook registers one.
- **Ambient discovery does not honour a redirected config directory.** Setting
  `PI_CODING_AGENT_DIR` did not make hooks under that directory load; project
  `.omp/hooks/post/*.ts` did. agentctl therefore passes the hook with an explicit
  `-e <path>`, which always loads and requires no edit to a user's config.
- `pi.sendMessage({customType, content, display}, {triggerTurn:true,
  deliverAs:"followUp"})` while idle started a real model turn that answered the
  injected text.
- `ctx.sessionManager.getSessionId()` returns the native session UUID.

## Pi 1.5.2 — measured extension facts

Same extension shape and event names as OMP (`input`, `turn_start`, `turn_end`,
`agent_start`, `agent_end`, `agent_settled`, `session_start`, `message_end`),
package `@earendil-works/pi-coding-agent`. Both `agent_end` and `agent_settled`
were observed in a TUI turn; `agent_settled` calling
`pi.sendMessage({triggerTurn:true, deliverAs:"followUp"})` while idle produced a
second real model turn.

## Claude Code 2.1.327 — measured hook facts

Hooks come from a settings file (`--settings <file>` merges with the user's own).
Verified payloads:

- `SessionStart` `{session_id, transcript_path, cwd, source:"startup"}`
- `UserPromptSubmit` `{session_id, prompt_id, permission_mode, prompt}`
- `Stop` `{session_id, prompt_id, stop_hook_active, last_assistant_message}`
- `SessionEnd` `{reason}`

Verified wake: a `Stop` hook printing
`{"decision":"block","reason":"[agentctl event] …"}` makes Claude continue the
conversation. The transcript showed the reason arriving as `Stop hook feedback:`
followed by a fresh assistant reply. `stop_hook_active` is `true` on the follow-up
`Stop`, which is the recursion guard agentctl honours.

**No hook fires at turn start.** `UserPromptSubmit` is the ingress signal;
`turn.started` is therefore not claimed for Claude.

## What agentctl does with each surface

| Job | omp | pi | claude |
| --- | --- | --- | --- |
| Hook asset | `-e <asset>.ts` | `-e <asset>.ts` | `--settings <fragment>.json` + script |
| Where the asset lives | `$AGENTCTL_HOME/integrations/omp/` | `…/pi/` | `…/claude/` |
| Records an observation | append to `$AGENTCTL_HOME/spool/omp.jsonl` | `…/pi.jsonl` | via the hook script → same spool |
| Who ingests | any CLI command, and agentctld each tick | same | same |

Nothing agentctl installs writes to a user's own harness configuration. A
workspace without an installed integration launches normally; its sends are
refused unless `--force`/`--unconfirmed` is given, and then recorded as
`delivery_unknown` rather than a fabricated success.

## Degradations stated plainly

- **Claude has no turn-start event.** Anything that needs "started" for Claude
  uses the message ACK as the proxy.
- **Codex and OpenCode have no adapter.** Sends are `delivery_unknown`; turn
  completion is invisible because codex emits no status record.
- **Hook identity is not a security boundary.** It prevents accidental
  cross-workspace routing, not a malicious local process running as the same
  Unix user.
