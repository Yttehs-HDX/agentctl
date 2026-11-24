# Testing

```sh
uv venv
uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The suite is hermetic: every test runs with `AGENTCTL_HOME` pointed at a
temporary directory, so the real `~/.config/agentctl` is never touched, and the
runtime tests use a fake `hc` script (see `tests/conftest.py`) instead of a live
Herdr Core.

| file | covers |
| --- | --- |
| `test_workspace.py` | canonical paths, stable ids across reopen, symlink identity, `..` escape |
| `test_config.py` | TOML round-trip, schema validation, empty default agent |
| `test_agents.py` | catalog resolution, disabled/missing/unknown agents, version verification |
| `test_runtime.py` | `hc` argv shape, verbatim sends, error-kind mapping, label filters |
| `test_session.py` | create, readiness, duplicate names, cwd policy, agent precedence, rollback, delete |
| `test_lifecycle.py` | status mapping, label-based terminal rediscovery, recovery rules |
| `test_messaging.py` | delivery proof, `--force` opt-out, keys, interrupt |
| `test_prune.py` | dry run, directory/row removal, live rows and outside paths kept |
| `test_recovery.py` | leader restart: same identity, same sessions, `recover`, isolation |
| `test_worktree.py` | worktree creation, branch naming, cleanup policy, non-git rejection |
| `test_cli.py` | JSON envelope and schema, `--json` argv handling, usage errors |

`conftest.py` supplies:

- `isolated_home` (autouse) — temporary `AGENTCTL_HOME`.
- `workspace` — a project directory with a `backend/` subdirectory.
- `fake_hc` — a Python stand-in for `hc` on `PATH`. It records every argv in
  `$FAKE_HC_STATE` and answers `run/ls/get/send/read/wait/kill/rm/server` with
  the same JSON shapes as hc 0.1.18, so adapter and service tests can assert on
  the exact command line without a live core.

## Manual end-to-end

Against a real Herdr Core (use a scratch core if you do not want to disturb your
own terminals):

```sh
export HC_SESSION=lab XDG_CONFIG_HOME=/tmp/l/xdg
agentctl setup
agentctl config set default-agent omp
agentctl --workspace=/tmp/proj init
agentctl --workspace=/tmp/proj create --name w1 --cwd /tmp/proj
agentctl --workspace=/tmp/proj send w1 "Reply OK, then stop."
agentctl --workspace=/tmp/proj wait w1
agentctl --workspace=/tmp/proj read w1
hc server restart                      # Leader/core restart
agentctl --workspace=/tmp/proj recover
agentctl --workspace=/tmp/proj list
```

Recovery of a lost terminal:

```sh
hc kill <terminal>; hc rm <terminal>
agentctl --workspace=/tmp/proj recover w1     # → resumed, uses native session id
agentctl --workspace=/tmp/proj status w1      # starting, then idle
```
