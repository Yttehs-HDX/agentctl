"""Typer CLI.

The CLI only parses arguments, calls one service and renders the result.
Database access, runtime calls and path validation live below it.
"""

from __future__ import annotations

import io
import json as jsonlib
import os
import sys
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Iterator, Optional

import typer

from agentctl import JSON_SCHEMA, __version__
from agentctl.agents.catalog import list_specs, resolve, version_verified
from agentctl.agents.detector import installed_command, version as agent_version
from agentctl.config import default_config, load_config, save_config
from agentctl.config.loader import dumps
from agentctl.config.schema import Config
from agentctl.errors import (
    EXIT_CONFLICT,
    AgentctlError,
    AgentUnknown,
    ConfigError,
    LeaderRequired,
    SetupRequired,
    UsageError,
)
from agentctl.leader.service import LeaderService
from agentctl.paths import config_path
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.messaging import MessagingService
from agentctl.skills import (
    SKILL_ID,
    InstallResult,
    install as install_skill,
    source_dir,
    status as skill_status,
    uninstall as uninstall_skill,
)
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_database
from agentctl.workspace.discovery import discover, init_target
from agentctl.workspace.resolver import canonical
from agentctl.workspace.service import WorkspaceService

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Workspace-scoped agent session manager on top of Herdr Core.",
)

agents_app = typer.Typer(no_args_is_help=True, help="Inspect the agent catalog.")
config_app = typer.Typer(no_args_is_help=True, help="Read and write global configuration.")
app.add_typer(agents_app, name="agents")
app.add_typer(config_app, name="config")

leader_app = typer.Typer(no_args_is_help=True, help="Where this workspace's Leader runs.")
app.add_typer(leader_app, name="leader")

skills_app = typer.Typer(
    no_args_is_help=True,
    help="Install the bundled Leader skill into .agents/skills.",
)
app.add_typer(skills_app, name="skills")

WORKSPACE_HELP = (
    "Leader workspace root. Optional: defaults to AGENTCTL_WORKSPACE, then the "
    "registered workspace at or above the current directory."
)


@dataclass(slots=True)
class State:
    workspace_arg: str | None = None
    json: bool = False
    config: Config | None = None
    config_error: Exception | None = None

    def emit(self, payload: Any, human) -> None:
        if self.json:
            body = payload if isinstance(payload, dict) else {"result": payload}
            typer.echo(jsonlib.dumps({"schema": JSON_SCHEMA, **body}, indent=2, default=str))
        else:
            human()

    def runtime(self) -> HerdrCoreAdapter:
        config = self.require_config()
        return HerdrCoreAdapter(config.runtime.hc_command, timeout=config.runtime.call_timeout_seconds)

    def require_config(self) -> Config:
        if self.config is not None:
            return self.config
        if self.config_error is not None:
            raise self.config_error
        try:
            self.config = load_config()
        except FileNotFoundError as exc:
            raise SetupRequired(
                "agentctl is not set up on this machine", "run `agentctl setup` once"
            ) from exc
        return self.config

    def env_workspace(self) -> str | None:
        """`AGENTCTL_WORKSPACE`.

        Read from the live environment rather than captured at import, so a
        test or wrapper can set it per call.
        """
        return os.environ.get("AGENTCTL_WORKSPACE") or None



def _state(ctx: typer.Context) -> State:
    state = ctx.obj
    if not isinstance(state, State):
        state = State()
        ctx.obj = state
    return state


@app.callback()
def main_callback(
    ctx: typer.Context,
    workspace: Annotated[Optional[str], typer.Option("--workspace", "-w", help=WORKSPACE_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    # `--workspace` is kept separate from AGENTCTL_WORKSPACE: discovery needs to
    # tell an explicit flag from an environment default for its `source` field,
    # and the flag must keep winning.
    state = State(workspace_arg=workspace or None, json=json_output)
    ctx.obj = state
    if ctx.invoked_subcommand in {"setup", "agents", "config", "skills", "version"}:
        return
    try:
        state.config = load_config()
    except FileNotFoundError:
        state.config_error = SetupRequired(
            "agentctl is not set up on this machine", "run `agentctl setup` once"
        )
    except ConfigError as exc:
        state.config_error = exc


@contextmanager
def _open(state: State) -> Iterator[tuple[Config, Store, HerdrCoreAdapter]]:
    config = state.require_config()
    with open_database() as conn:
        store = Store(conn)
        yield config, store, state.runtime()


def _workspace(state: State, store: Store, *, allow_missing: bool = False):
    """Resolve this command's workspace (flag > env > cwd > nearest parent)."""
    return discover(
        store,
        explicit=state.workspace_arg,
        env=state.env_workspace(),
        allow_missing=allow_missing,
    ).workspace


# ----------------------------------------------------------------------- setup


@app.command()
def setup(
    ctx: typer.Context,
    default_agent: Annotated[
        Optional[str], typer.Option("--default-agent", help="Global default agent; omit to leave unset.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Rewrite config.toml even if it exists.")] = False,
) -> None:
    """One-time machine setup: check HC, detect agents, write config.toml."""
    state = _state(ctx)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        try:
            config = load_config()
        except ConfigError:
            config = default_config()
    else:
        config = default_config()

    runtime = HerdrCoreAdapter(config.runtime.hc_command)
    hc_installed = runtime.available()
    hc_version: str | None = None
    if hc_installed:
        try:
            hc_version = runtime.version()
        except AgentctlError as exc:
            hc_version = f"error: {exc.message}"

    rows = []
    for spec in list_specs(config):
        definition = _definition(spec.id)
        is_installed = bool(spec.extra.get("installed"))
        row_version = agent_version(definition) if is_installed and definition else None
        rows.append(
            {
                "id": spec.id,
                "command": spec.command,
                "installed": is_installed,
                "version": row_version,
                "version_verified": version_verified(definition, row_version) if definition else None,
                "enabled": spec.enabled,
                "integration": spec.integration,
            }
        )

    chosen = default_agent
    if chosen is None:
        installed_ids = [row["id"] for row in rows if row["installed"] and row["enabled"]]
        if sys.stdin.isatty() and installed_ids and not _non_interactive(state):
            chosen = _prompt_default_agent(installed_ids, current=config.defaults.agent)
        else:
            # Non-interactive: only pick a default when there is no ambiguity.
            chosen = installed_ids[0] if len(installed_ids) == 1 else (config.defaults.agent or "")
    if chosen:
        try:
            resolve(config, chosen)
        except AgentctlError as exc:
            raise UsageError(f"cannot use {chosen!r} as the default agent: {exc.message}", exc.hint) from exc
    config.defaults.agent = chosen
    written = save_config(config)

    payload = {
        "config": str(written),
        "runtime": {"installed": hc_installed, "version": hc_version},
        "agents": rows,
        "default_agent": chosen or None,
    }
    state.emit(payload, lambda: _print_setup(payload))


def _definition(agent_id: str):
    from agentctl.agents.definitions import AGENT_DEFINITIONS

    return AGENT_DEFINITIONS.get(agent_id)


def _non_interactive(state: "State") -> bool:
    return state.json or os.environ.get("AGENTCTL_NONINTERACTIVE") == "1"


def _prompt_default_agent(installed_ids: list[str], *, current: str) -> str:
    typer.echo("")
    typer.echo("Choose the global default agent:")
    for index, agent_id in enumerate(installed_ids, start=1):
        marker = " (current)" if agent_id == current else ""
        typer.echo(f"  {index}) {agent_id}{marker}")
    typer.echo(f"  {len(installed_ids) + 1}) no default (always pass --agent)")
    while True:
        answer = typer.prompt(
            f"Number [1-{len(installed_ids) + 1}]",
            default=str(installed_ids.index(current) + 1) if current in installed_ids else "1",
        ).strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(installed_ids):
                return installed_ids[index - 1]
            if index == len(installed_ids) + 1:
                return ""
        typer.echo("please enter one of the listed numbers")


def _print_setup(payload: dict[str, Any]) -> None:
    runtime = payload["runtime"]
    typer.echo(f"config: {payload['config']}")
    if runtime["installed"]:
        typer.echo(f"herdr core: hc {runtime['version']}")
    else:
        typer.echo("herdr core: MISSING - install Herdr Core and rerun setup")
    typer.echo("")
    typer.echo(f"{'agent':<10}{'status':<24}{'status words':<14}command")
    for row in payload["agents"]:
        status = "installed" if row["installed"] else "missing"
        if row["installed"] and row["version_verified"] is False:
            status += " (unverified)"
        elif row["installed"] and row["version_verified"] is None:
            status += " (unmeasured)"
        if not row["enabled"]:
            status += " (off)"
        word = "reports" if row["integration"] else "none"
        typer.echo(f"{row['id']:<10}{status:<24}{word:<14}{' '.join(row['command'])}")
    typer.echo("")
    typer.echo(f"default agent: {payload['default_agent'] or '(none)'}")


# ---------------------------------------------------------------- workspace ops


@app.command()
def init(ctx: typer.Context) -> None:
    """Create (or load) the persistent workspace identity for --workspace."""
    state = _state(ctx)
    with _open(state) as (_config, store, runtime):
        root, _source = init_target(explicit=state.workspace_arg, env=state.env_workspace())
        workspace, created = WorkspaceService(store, runtime).init(root)
        state.emit(
            {"workspace": workspace.to_dict(), "created": created},
            lambda: typer.echo(
                f"{'initialized' if created else 'already initialized'}: {workspace.root_path}\n"
                f"  workspace id: {workspace.id}\n"
                f"  leader id:    {workspace.leader_id}"
            ),
        )


@app.command()
def info(ctx: typer.Context) -> None:
    """Show workspace identity, session counts and runtime status."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        service = WorkspaceService(store, runtime, config)
        workspace = _workspace(state, store)
        body = service.info(workspace)
        state.emit(body.to_dict(), lambda: _print_info(body))


def _print_info(body) -> None:
    typer.echo(f"workspace id:  {body.workspace.id}")
    typer.echo(f"leader id:     {body.workspace.leader_id}")
    typer.echo(f"root:          {body.workspace.root_path}")
    typer.echo(f"sessions:      {body.session_count} total, {body.active_count} active")
    typer.echo(f"default agent: {body.default_agent or '(none)'}")
    if body.runtime_available:
        typer.echo(f"runtime:       herdr-core hc {body.runtime_version}")
    else:
        typer.echo("runtime:       MISSING (hc not on PATH)")
    if body.orphan_terminals:
        typer.echo(f"orphans:       {', '.join(body.orphan_terminals)}")


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Also show sessions deleted earlier.")
    ] = False,
    no_refresh: Annotated[
        bool, typer.Option("--no-refresh", help="Skip the runtime status refresh.")
    ] = False,
) -> None:
    """List the sessions of this workspace."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        if all_sessions:
            # Deleted rows are history: never re-derive them from the runtime.
            sessions = store.sessions.list_for_workspace(workspace.id, include_deleted=True)
        else:
            sessions = service.list(workspace, refresh=not no_refresh)
        payload = {"workspace_id": workspace.id, "sessions": [s.to_dict() for s in sessions]}
        state.emit(payload, lambda: _print_sessions(sessions))


def _print_sessions(sessions) -> None:
    if not sessions:
        typer.echo("no sessions in this workspace")
        return
    typer.echo(f"{'name':<20}{'agent':<10}{'status':<12}{'mode':<9}{'id':<38}cwd")
    for session in sessions:
        typer.echo(
            f"{session.name:<20}{session.agent:<10}{session.status:<12}"
            f"{session.mode:<9}{session.id:<38}{session.cwd}"
        )


@app.command()
def rebind(
    ctx: typer.Context,
    new_root: Annotated[str, typer.Argument(help="New canonical root for this workspace identity.")],
) -> None:
    """Move this workspace identity to another directory (explicit migration)."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        service = WorkspaceService(store, runtime, config)
        workspace = _workspace(state, store, allow_missing=True)
        target = canonical(new_root)
        before = workspace.root_path
        moved = service.rebind(workspace, target)
        state.emit(
            {"workspace": moved.to_dict(), "previous_root": before},
            lambda: typer.echo(
                f"workspace {moved.id} moved\n  from: {before}\n  to:   {moved.root_path}"
            ),
        )


@app.command()
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n", help="Session name, unique in this workspace.")],
    cwd: Annotated[str, typer.Option("--cwd", help="Working directory for the worker.")],
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent id; defaults to config.")] = None,
    mode: Annotated[Optional[str], typer.Option("--mode", "-m", help="shared or worktree.")] = None,
    base_ref: Annotated[
        Optional[str], typer.Option("--base-ref", help="Git ref to branch from for --mode worktree.")
    ] = None,
    wait: Annotated[
        Optional[float],
        typer.Option("--wait", help="Seconds to wait for the agent's first status report."),
    ] = None,
    task: Annotated[
        Optional[str],
        typer.Option("--task", help="First message, sent as the agent starts."),
    ] = None,
) -> None:
    """Create a worker session."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        result = SessionService(store, runtime, config).create(
            workspace,
            CreateRequest(
                name=name, agent=agent, cwd=cwd, mode=mode, base_ref=base_ref, task=task
            ),
            wait_timeout=wait or 0.0,
        )
        session = result.session
        payload = {
            "session": session.to_dict(),
            "terminal": result.terminal.address,
            "native_session_id": result.native_session_id,
            "ready": result.ready,
        }
        state.emit(
            payload,
            lambda: typer.echo(
                f"{session.id}\n"
                f"  name:     {session.name}\n"
                f"  agent:    {session.agent}\n"
                f"  cwd:      {session.cwd}\n"
                f"  mode:     {session.mode}\n"
                f"  terminal: {result.terminal.address}\n"
                f"  ready:    {'yes' if result.ready else 'no'}"
            ),
        )


@app.command()
def delete(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    keep_worktree: Annotated[bool, typer.Option("--keep-worktree", help="Leave the git worktree.")] = False,
) -> None:
    """Terminate a session and remove it from the registry."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        deleted = SessionService(store, runtime, config).delete(
            workspace, session, keep_worktree=keep_worktree
        )
        state.emit(
            {"session": deleted.to_dict()},
            lambda: typer.echo(f"deleted {deleted.name} ({deleted.id})"),
        )


@app.command()
def prune(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be removed without deleting.")
    ] = False,
) -> None:
    """Remove agentctl-owned state of soft-deleted sessions."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        result = SessionService(store, runtime, config).prune(workspace, dry_run=dry_run)
        state.emit(result.to_dict(), lambda: _print_prune(result))
        if result.errors:
            raise typer.Exit(code=1)


def _format_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    return f"{count / 1024:.1f} KiB"


def _print_prune(result) -> None:
    if result.dry_run:
        typer.echo(
            f"would prune {result.sessions} sessions, "
            f"{result.directories} directories, {_format_bytes(result.bytes_freed)}"
        )
    else:
        typer.echo(
            f"pruned {result.sessions} sessions, "
            f"removed {result.directories} directories, freed {_format_bytes(result.bytes_freed)}"
        )
    for note in result.notes:
        typer.echo(f"note: {note}")
    for error in result.errors:
        typer.echo(f"kept: {error}")


# --------------------------------------------------------------------- messaging

# --------------------------------------------------------------------- messaging


@app.command()
def send(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    message: Annotated[str, typer.Argument(help="Text to send, or - to read stdin.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Type even if the agent is blocked or still working (Herdr refuses by default).",
        ),
    ] = False,
    wait: Annotated[
        bool, typer.Option("--wait", help="Then wait for the turn this send started.")
    ] = False,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="With --wait: seconds before a check-in, not a failure."),
    ] = None,
) -> None:
    """Send text to a session.

    Herdr delivers the text, guards the delivery and reports whether the turn
    started. agentctl adds no queue: the harness owns its own input queue, and
    the message reaches the agent exactly as if it had been typed.
    """
    state = _state(ctx)
    text = _read_message_text(message)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        report = MessagingService(runtime, service.lifecycle).send(
            target, text, force=force, wait=wait, timeout=timeout
        )
        service.lifecycle.refresh(target)
        service.sync_native_id(target)
        payload = report.to_dict()
        state.emit(payload, lambda: _print_send(payload))


def _read_message_text(message: str) -> str:
    if message == "-":
        if sys.stdin.isatty():
            raise UsageError(
                "send - expects piped input",
                "pipe the text, e.g. printf '%s' \"$text\" | agentctl send NAME -",
            )
        text = sys.stdin.read()
    else:
        text = message
    if not text.strip():
        raise UsageError("refusing to send an empty message")
    return text


def _print_send(payload: dict[str, Any]) -> None:
    line = f"sent to {payload['session']}"
    if payload.get("turn") is not None:
        line += f" at turn {payload['turn']}"
    if payload.get("started") is False:
        line += " (not started: the agent did not begin a turn)"
    typer.echo(line)
    turn = payload.get("wait")
    if turn:
        if turn["ended"]:
            typer.echo(f"  turn ended: {turn['status'] or turn['reason']}")
        else:
            typer.echo(f"  still {turn['status'] or 'working'} (check-in, not a failure)")


@app.command()
def keys(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    keys: Annotated[list[str], typer.Argument(help="Key names: enter, esc, tab, down, ctrl+c, f5, a, ...")],
) -> None:
    """Send named keys to a session (answer a trust or permission dialog)."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        report = MessagingService(runtime, service.lifecycle).keys(target, keys)
        state.emit(report.to_dict(), lambda: typer.echo(f"sent keys to {report.session} (turn {report.turn})"))


@app.command()
def interrupt(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    timeout: Annotated[
        Optional[float], typer.Option("--timeout", help="Seconds to wait for the interrupt to land.")
    ] = None,
) -> None:
    """Interrupt the agent in a session (ctrl+c)."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        report = MessagingService(runtime, service.lifecycle).interrupt(target, timeout=timeout)
        state.emit(
            report.to_dict(),
            lambda: typer.echo(f"{report.session}: {report.key or '-'} -> {report.outcome or '-'}"),
        )


@app.command()
def read(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    lines: Annotated[int, typer.Option("--lines", "-l", help="History lines above the screen.")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing while it changes.")] = False,
) -> None:
    """Print a session's screen and recent history."""
    state = _state(ctx)
    if follow and state.json:
        raise UsageError(
            "--follow cannot be combined with --json",
            "call read without --follow for a single JSON snapshot",
        )
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        messaging = MessagingService(runtime, service.lifecycle)
        if follow:
            messaging.follow(target, lines=lines)
            return
        output = messaging.read(target, lines=lines)
        if state.json:
            typer.echo(
                jsonlib.dumps(
                    {
                        "schema": JSON_SCHEMA,
                        "text": output.text,
                        "complete": output.complete,
                        "how": output.how,
                        "why": output.why,
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(output.text.rstrip("\n"))
            if output.why:
                typer.echo(f"[agentctl] partial: screen only ({output.why})", file=sys.stderr)


@app.command()
def status(
    ctx: typer.Context,
    session: Annotated[Optional[str], typer.Argument(help="Session name or id; omit for all.")] = None,
    no_refresh: Annotated[
        bool, typer.Option("--no-refresh", help="Skip the runtime status refresh.")
    ] = False,
) -> None:
    """Show one session's or the whole workspace's status."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        if session:
            target = service.get(workspace, session)
            if not no_refresh:
                service.lifecycle.refresh(target)
            state.emit(
                {"session": target.to_dict()},
                lambda: typer.echo(
                    f"{target.name}: {target.status}"
                    + (f" ({target.last_error})" if target.last_error else "")
                ),
            )
        else:
            sessions = service.list(workspace, refresh=not no_refresh)
            state.emit(
                {"workspace_id": workspace.id, "sessions": [s.to_dict() for s in sessions]},
                lambda: _print_sessions(sessions),
            )


@app.command()
def wait(
    ctx: typer.Context,
    session: Annotated[
        Optional[str], typer.Argument(help="Session name or id; omit with --all/--any.")
    ] = None,
    all_workers: Annotated[
        bool, typer.Option("--all", help="Wait until none of the bound Leader's children is working.")
    ] = False,
    any_worker: Annotated[
        bool, typer.Option("--any", help="Wait for news from the bound Leader's children.")
    ] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds before a check-in (not failure).")] = 180.0,
) -> None:
    """Wait for a Worker's turn, or for the Workers' next news.

    `wait NAME` waits for one session. `--any` and `--all` only wait on the
    current Leader's Herdr children. They do not include parentless Workers or
    Workers that still belong to a previous Leader.
    """
    state = _state(ctx)
    if all_workers and any_worker:
        raise UsageError("--all and --any are alternatives", "pass one of them")
    children_mode = "all" if all_workers else ("any" if any_worker else None)
    if children_mode is not None and session is not None:
        raise UsageError("pass a session name or --all/--any, not both")
    if children_mode is None and session is None:
        raise UsageError(
            "wait needs a session, or --all/--any",
            "agentctl wait NAME, or agentctl wait --any for the next Worker's news",
        )
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        if children_mode is not None:
            leader = LeaderService(store, runtime).status(workspace)
            if not leader.live or leader.terminal is None:
                raise LeaderRequired(
                    "waiting for children requires a live Leader terminal",
                    "run `agentctl leader attach` from the Leader's terminal or use `agentctl wait NAME`",
                )
            parent = leader.terminal.address
            outcome = runtime.wait_children(parent=parent, mode=children_mode, timeout=timeout)
            payload = {
                "mode": f"children:{children_mode}",
                "scope": "leader_children",
                "parent": parent,
                "ended": outcome.ended,
                "reason": outcome.reason,
                "status": outcome.status,
                "turn": outcome.turn,
                "turn_ended": outcome.turn_ended,
                "check_in": outcome.timed_out,
                "children": (outcome.raw or {}).get("children") or [],
            }
            state.emit(payload, lambda: _print_children_wait(payload))
            return

        service = SessionService(store, runtime, config)
        messaging = MessagingService(runtime, service.lifecycle)
        target = service.get(workspace, session)
        outcome = messaging.wait(target, timeout=timeout)
        service.lifecycle.refresh(target)
        service.sync_native_id(target)
        payload = {
            "session": target.name,
            "ended": outcome.ended,
            "reason": outcome.reason,
            "status": outcome.status,
            "exit_code": outcome.exit_code,
            "turn": outcome.turn,
            "turn_ended": outcome.turn_ended,
            "check_in": outcome.timed_out,
            "session_status": target.status,
        }
        state.emit(payload, lambda: _print_wait(payload))


def _print_wait(payload: dict[str, Any]) -> None:
    if payload["ended"]:
        typer.echo(f"{payload['session']} stopped: {payload['status'] or payload['reason']}")
    else:
        typer.echo(f"{payload['session']} still {payload['status'] or 'working'} (check-in)")


def _print_children_wait(payload: dict[str, Any]) -> None:
    label = f"Leader {payload['parent']}'s children"
    if payload["check_in"]:
        typer.echo(f"{label}: check-in ({payload['reason']}, {len(payload['children'])} children)")
        return
    if not payload["children"]:
        typer.echo(f"{label}: no children returned (not a workspace-wide result)")
        return
    typer.echo(f"{label}:")
    for child in payload["children"]:
        typer.echo(
            f"{child.get('terminal')}: {child.get('status') or '-'} "
            f"turn {child.get('turn')} ({child.get('reason')})"
        )


@app.command()
def recover(
    ctx: typer.Context,
    session: Annotated[Optional[str], typer.Argument(help="Session name or id; omit for all.")] = None,
) -> None:
    """Re-discover live terminals, or relaunch sessions from their native ids."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        if session:
            results = [service.recover(workspace, session)]
        else:
            results = service.recover_workspace(workspace)
        payload = {
            "workspace_id": workspace.id,
            "results": [
                {"session": s.name, "id": s.id, "result": outcome, "status": s.status}
                for s, outcome in results
            ],
        }
        state.emit(
            payload,
            lambda: [typer.echo(f"{s.name}: {outcome} ({s.status})") for s, outcome in results],
        )


# --------------------------------------------------------------------- leader


@leader_app.command("attach")
def leader_attach(
    ctx: typer.Context,
    terminal: Annotated[
        Optional[str],
        typer.Option("--terminal", help="Herdr terminal leading this workspace; defaults to $HC_TERMINAL_ID."),
    ] = None,
    session_id: Annotated[
        Optional[str], typer.Option("--native-session", help="The harness's own session id, for recovery.")
    ] = None,
    takeover: Annotated[
        bool, typer.Option("--takeover", help="Replace a binding whose terminal is known to be gone.")
    ] = False,
) -> None:
    """Record where this workspace's Leader runs."""
    state = _state(ctx)
    with _open(state) as (_config, store, runtime):
        workspace = _workspace(state, store)
        service = LeaderService(store, runtime)
        want = terminal or os.environ.get("HC_TERMINAL_ID") or None
        binding = service.attach(
            workspace, terminal=want, native_session_id=session_id, take_over=takeover
        )
        native = binding.native_session_id or "(none)"
        payload = {"binding": binding.to_dict()}
        state.emit(
            payload,
            lambda: typer.echo(
                f"leader of {workspace.id} attached\n"
                f"  terminal: {binding.terminal or '(outside Herdr)'}\n"
                f"  native:   {native}"
            ),
        )


@leader_app.command("detach")
def leader_detach(ctx: typer.Context) -> None:
    """Forget the binding. The Leader's terminal keeps running."""
    state = _state(ctx)
    with _open(state) as (_config, store, runtime):
        workspace = _workspace(state, store)
        removed = LeaderService(store, runtime).detach(workspace)
        state.emit({"detached": removed}, lambda: typer.echo("leader detached" if removed else "no binding"))


@leader_app.command("status")
def leader_status(ctx: typer.Context) -> None:
    """Show the binding and whether Herdr still reports that terminal live."""
    state = _state(ctx)
    with _open(state) as (_config, store, runtime):
        workspace = _workspace(state, store)
        status = LeaderService(store, runtime).status(workspace)
        body = status.to_dict()
        state.emit(body, lambda: _print_leader_status(body))


def _print_leader_status(body: dict[str, Any]) -> None:
    binding = body.get("binding")
    if not binding:
        typer.echo(body["reason"] or "no leader attached")
        return
    typer.echo(f"terminal: {binding.get('terminal') or '(outside Herdr)'}")
    typer.echo(f"native:   {binding.get('native_session_id') or '(none)'}")
    terminal = body.get("terminal")
    if terminal:
        typer.echo(f"state:    {terminal['state']} ({terminal['status'] or 'no status word'})")
    typer.echo(f"live:     {'yes' if body['live'] else 'no'}")
    if body.get("reason"):
        typer.echo(f"note:     {body['reason']}")


# ------------------------------------------------------------------------ agents


@agents_app.command("list")
def agents_list(ctx: typer.Context) -> None:
    """Show configured agents and what this machine supports."""
    state = _state(ctx)
    config = _config_or_default()
    rows = []
    for spec in list_specs(config):
        definition = _definition(spec.id)
        is_installed = bool(spec.extra.get("installed"))
        row_version = agent_version(definition) if is_installed and definition else None
        rows.append(
            {
                "id": spec.id,
                "command": spec.command,
                "enabled": spec.enabled,
                "installed": is_installed,
                "version": row_version,
                "version_verified": version_verified(definition, row_version) if definition else None,
                "integration": spec.integration,
                "supports_native_session": spec.supports_native_session,
                "supports_resume": spec.supports_resume,
                "notes": spec.notes,
            }
        )
    state.emit({"agents": rows}, lambda: _print_agents(rows))


def _config_or_default() -> Config:
    try:
        return load_config()
    except FileNotFoundError:
        return default_config()


def _print_agents(rows) -> None:
    typer.echo(f"{'id':<12}{'enabled':<9}{'installed':<11}{'verified':<10}{'words':<8}{'resume':<8}command")
    for row in rows:
        verified = "-" if row["version_verified"] is None else ("yes" if row["version_verified"] else "no")
        typer.echo(
            f"{row['id']:<12}{str(row['enabled']).lower():<9}{str(row['installed']).lower():<11}"
            f"{verified:<10}"
            f"{'reports' if row['integration'] else 'none':<8}"
            f"{str(row['supports_resume']).lower():<8}{' '.join(row['command'])}"
        )


@agents_app.command("inspect")
def agents_inspect(
    ctx: typer.Context,
    agent_id: Annotated[str, typer.Argument(help="Agent id from `agents list`.")],
) -> None:
    """Show one agent's measured capabilities."""
    state = _state(ctx)
    config = _config_or_default()
    definition = _definition(agent_id)
    entry = config.agents.get(agent_id)
    if entry is None and definition is None:
        raise AgentUnknown(
            f"agent {agent_id!r} is not in the catalog",
            "run `agentctl agents list` to see configured agents",
        )
    command = entry.command if entry else (definition.command if definition else None)
    installed_version = agent_version(definition) if definition else None
    payload = {
        "id": agent_id,
        "configured": entry is not None,
        "enabled": entry.enabled if entry else None,
        "command": command,
        "installed": installed_command(command) if command else False,
        "version": installed_version,
        "version_verified": version_verified(definition, installed_version) if definition else None,
        "integration": bool(definition and definition.integration),
        "supports_native_session": bool(definition and definition.session_id != "none"),
        "session_id_strategy": definition.session_id if definition else None,
        "supports_resume": bool(definition and definition.supports_resume),
        "resume_verified": bool(definition and definition.resume_verified),
        "notes": definition.notes if definition else "no verified integration",
    }
    state.emit(payload, lambda: _print_agent_detail(payload))


def _print_agent_detail(payload: dict[str, Any]) -> None:
    for key in (
        "id",
        "command",
        "configured",
        "enabled",
        "installed",
        "version",
        "version_verified",
        "integration",
        "supports_native_session",
        "session_id_strategy",
        "supports_resume",
        "resume_verified",
        "notes",
    ):
        typer.echo(f"{key:<24}{payload[key]}")


# ------------------------------------------------------------------------ config


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="default-agent, session.default-mode, agents.<id>.enabled")],
    value: Annotated[str, typer.Argument(help="Value to store.")],
) -> None:
    """Set one configuration value."""
    state = _state(ctx)
    config = _must_load_config()
    _apply_set(config, key, value)
    path = save_config(config)
    state.emit({"config": str(path), "key": key, "value": value}, lambda: typer.echo(f"{key} = {value}"))


def _apply_set(config: Config, key: str, value: str) -> None:
    if key == "default-agent":
        if value:
            resolve(config, value)
        config.defaults.agent = value
        return
    if key == "session.default-mode":
        if value not in ("shared", "worktree"):
            raise UsageError("session.default-mode must be shared or worktree")
        config.session.default_mode = value  # type: ignore[assignment]
        return
    if key.startswith("agents.") and key.endswith(".enabled"):
        agent_id = key.removeprefix("agents.").removesuffix(".enabled")
        entry = config.agents.get(agent_id)
        if entry is None:
            raise UsageError(
                f"agent {agent_id!r} is not configured", "add it under [agents.<id>] in config.toml"
            )
        lower = value.lower()
        if lower not in ("true", "false"):
            raise UsageError("enabled values are true or false")
        entry.enabled = lower == "true"
        return
    raise UsageError(f"unknown configuration key {key!r}")


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="default-agent, session.default-mode, agents.<id>.enabled")],
) -> None:
    """Print one configuration value."""
    state = _state(ctx)
    value = _read_key(_must_load_config(), key)
    state.emit({"key": key, "value": value}, lambda: typer.echo(value if value is not None else ""))


def _read_key(config: Config, key: str) -> Any:
    if key == "default-agent":
        return config.defaults.agent or None
    if key == "session.default-mode":
        return config.session.default_mode
    if key.startswith("agents.") and key.endswith(".enabled"):
        agent_id = key.removeprefix("agents.").removesuffix(".enabled")
        entry = config.agents.get(agent_id)
        if entry is None:
            raise UsageError(f"agent {agent_id!r} is not configured")
        return entry.enabled
    raise UsageError(f"unknown configuration key {key!r}")


@config_app.command("list")
def config_list(ctx: typer.Context) -> None:
    """Print the whole configuration as TOML."""
    state = _state(ctx)
    config = _must_load_config()
    if state.json:
        body = {"schema": JSON_SCHEMA, **config.model_dump(mode="json")}
        typer.echo(jsonlib.dumps(body, indent=2))
    else:
        typer.echo(dumps(config), nl=False)


def _must_load_config() -> Config:
    try:
        return load_config()
    except FileNotFoundError as exc:
        raise SetupRequired("agentctl is not set up on this machine", "run `agentctl setup` once") from exc


# ------------------------------------------------------------------------ skills

SCOPE_HELP = "Use ~/.agents/skills (default)."
PROJECT_HELP = "Use .agents/skills in the current directory."


def _skill_target(user: bool, project: bool) -> tuple[str, Path]:
    if user and project:
        raise UsageError("--user and --project are alternatives", "pass only one of them")
    scope, base = ("project", Path.cwd()) if project else ("user", Path.home())
    return scope, base / ".agents" / "skills" / SKILL_ID


def _emit_skill_result(
    state: State, source: Path, scope: str, path: Path, row: dict[str, str], summary: str
) -> None:
    def human() -> None:
        typer.echo(f"{scope}: {summary} {path}")
        if error := row.get("error"):
            typer.echo(f"  {error}", err=True)

    state.emit(
        {
            "skill": SKILL_ID,
            "source": str(source),
            "targets": [{"scope": scope, "path": str(path), **row}],
        },
        human,
    )


def _emit_skill_action(
    state: State, source: Path, scope: str, path: Path, result: InstallResult
) -> None:
    row = {"action": result.action}
    if result.error:
        row["error"] = result.error
    _emit_skill_result(state, source, scope, path, row, result.action)
    if result.action == "skipped":
        raise typer.Exit(code=EXIT_CONFLICT)


@skills_app.command("path")
def skills_path(ctx: typer.Context) -> None:
    """Print the bundled Leader skill's directory."""
    state = _state(ctx)
    source = source_dir()
    state.emit({"skill": SKILL_ID, "source": str(source)}, lambda: typer.echo(str(source)))


@skills_app.command("install")
def skills_install(
    ctx: typer.Context,
    user: Annotated[bool, typer.Option("--user", help=SCOPE_HELP)] = False,
    project: Annotated[bool, typer.Option("--project", help=PROJECT_HELP)] = False,
    copy: Annotated[bool, typer.Option("--copy", help="Copy the directory instead of linking it.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Replace an entry agentctl did not create.")] = False,
) -> None:
    """Install the bundled Leader skill into .agents/skills."""
    state = _state(ctx)
    scope, path = _skill_target(user, project)
    source = source_dir()
    result = install_skill(path, source, copy=copy, force=force)
    _emit_skill_action(state, source, scope, path, result)


@skills_app.command("status")
def skills_status(
    ctx: typer.Context,
    user: Annotated[bool, typer.Option("--user", help=SCOPE_HELP)] = False,
    project: Annotated[bool, typer.Option("--project", help=PROJECT_HELP)] = False,
) -> None:
    """Report the target as linked, copied, missing, or foreign."""
    state = _state(ctx)
    scope, path = _skill_target(user, project)
    source = source_dir()
    status = skill_status(path, source)
    _emit_skill_result(state, source, scope, path, {"state": status}, status)


@skills_app.command("uninstall")
def skills_uninstall(
    ctx: typer.Context,
    user: Annotated[bool, typer.Option("--user", help=SCOPE_HELP)] = False,
    project: Annotated[bool, typer.Option("--project", help=PROJECT_HELP)] = False,
) -> None:
    """Remove the installed skill if agentctl owns it."""
    state = _state(ctx)
    scope, path = _skill_target(user, project)
    source = source_dir()
    result = uninstall_skill(path, source)
    _emit_skill_action(state, source, scope, path, result)


# ----------------------------------------------------------------------- version


@app.command()
def version() -> None:
    """Print the agentctl version."""
    typer.echo(f"agentctl {__version__}")


# -------------------------------------------------------------------------- main


def _prepare_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Hoist `--json` before the command, stopping at the `--` terminator.

    Typer parses callback options before the command name, so a trailing
    `--json` would become a positional argument. Everything after `--` is
    caller data and is never touched, which makes a literal `--json` message
    sendable as `send NAME -- --json`.
    """
    out: list[str] = []
    json_mode = False
    for index, arg in enumerate(argv):
        if arg == "--":
            out.extend(argv[index:])
            break
        if arg == "--json":
            json_mode = True
            continue
        out.append(arg)
    if json_mode:
        out.insert(0, "--json")
    return out, json_mode


def main() -> None:
    argv, json_mode = _prepare_argv(sys.argv[1:])
    sys.argv = [sys.argv[0], *argv]
    try:
        if json_mode:
            # Click prints its own usage errors and exits; capture that text so
            # the machine-readable mode emits one JSON envelope instead.
            captured = io.StringIO()
            try:
                with redirect_stderr(captured):
                    app()
            except SystemExit as exc:
                if exc.code == 2:
                    _print_usage_error(captured.getvalue())
                raise
        else:
            app()
    except AgentctlError as exc:
        _print_error(exc, json_mode)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - one envelope for every failure
        # Click's standalone mode turns an escaping exception into exit 1 with
        # only a message, which breaks the promise that `--json` always yields
        # the documented envelope. Re-raise the ones that carry their own exit
        # status and wrap everything else as a generic error.
        if isinstance(exc, SystemExit):
            raise
        _print_error(AgentctlError(str(exc) or type(exc).__name__), json_mode)


def _print_usage_error(text: str) -> None:
    """Turn Click's usage error into the same JSON envelope as our errors."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    message = next((line.strip("│ ") for line in lines if line.startswith("│")), None)
    if message is None:
        message = next(
            (line.removeprefix("Error:").strip() for line in reversed(lines) if line.startswith("Error:")),
            None,
        )
    if message is None:
        message = lines[0] if lines else "invalid command line"
    body = {
        "schema": JSON_SCHEMA,
        "error": {
            "type": "UsageError",
            "code": "usage",
            "message": message,
            "hint": "run `agentctl <command> --help`",
        },
    }
    typer.echo(jsonlib.dumps(body, indent=2), err=True)


def _print_error(exc: AgentctlError, json_output: bool) -> None:
    if json_output:
        typer.echo(jsonlib.dumps(exc.to_dict(), indent=2), err=True)
    else:
        typer.echo(f"error: {exc.message}", err=True)
        if exc.hint:
            typer.echo(f"hint:  {exc.hint}", err=True)
    sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
