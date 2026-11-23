"""Typer CLI.

The CLI only parses arguments, calls one service and renders the result.
Database access, runtime calls and path validation live below it.
"""

from __future__ import annotations

import json as jsonlib
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Iterator, Optional

import typer

from agentctl import __version__
from agentctl.agents.catalog import list_specs, resolve, sync_catalog
from agentctl.agents.detector import installed, version as agent_version
from agentctl.config import default_config, load_config, save_config
from agentctl.config.loader import dumps
from agentctl.config.schema import Config
from agentctl.errors import (
    AgentctlError,
    ConfigError,
    SetupRequired,
    UsageError,
)
from agentctl.paths import config_path
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.messaging import MessagingService
from agentctl.session.service import CreateRequest, SessionService
from agentctl.storage import Store, open_database
from agentctl.workspace.resolver import resolve_root
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

WORKSPACE_HELP = "Leader workspace root; required by session and workspace commands."


@dataclass(slots=True)
class State:
    workspace_arg: str | None = None
    json: bool = False
    config: Config | None = None
    config_error: Exception | None = None

    def emit(self, payload: Any, human) -> None:
        if self.json:
            typer.echo(jsonlib.dumps(payload, indent=2, default=str))
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

    def root(self) -> str:
        return resolve_root(self.workspace_arg)


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
    state = State(workspace_arg=workspace, json=_json_requested(json_output))
    ctx.obj = state
    if ctx.invoked_subcommand in {"setup", "agents", "config", "version"}:
        return
    try:
        state.config = load_config()
    except FileNotFoundError:
        state.config_error = SetupRequired(
            "agentctl is not set up on this machine", "run `agentctl setup` once"
        )
    except ConfigError as exc:
        state.config_error = exc


def _json_requested(callback_value: bool) -> bool:
    """`--json` works both before and after the subcommand.

    `main()` hoists every occurrence to the front of argv and exports
    AGENTCTL_JSON, because Typer parses callback options before the command.
    """
    return callback_value or os.environ.get("AGENTCTL_JSON") == "1"


@contextmanager
def _open(state: State) -> Iterator[tuple[Config, Store, HerdrCoreAdapter]]:
    config = state.require_config()
    with open_database() as conn:
        store = Store(conn)
        sync_catalog(conn, config)
        yield config, store, state.runtime()


def _workspace(state: State, store: Store):
    return WorkspaceService(store, state.runtime()).require(state.root())


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
        is_installed = installed(definition) if definition else False
        rows.append(
            {
                "id": spec.id,
                "command": spec.command,
                "installed": is_installed,
                "version": agent_version(definition) if is_installed else None,
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

    with open_database() as conn:
        sync_catalog(conn, config)

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
    typer.echo(f"{'agent':<10}{'status':<14}{'status words':<14}command")
    for row in payload["agents"]:
        status = "installed" if row["installed"] else "missing"
        if not row["enabled"]:
            status += " (off)"
        word = "reports" if row["integration"] else "none"
        typer.echo(f"{row['id']:<10}{status:<14}{word:<14}{' '.join(row['command'])}")
    typer.echo("")
    typer.echo(f"default agent: {payload['default_agent'] or '(none)'}")


# ---------------------------------------------------------------- workspace ops


@app.command()
def init(ctx: typer.Context) -> None:
    """Create (or load) the persistent workspace identity for --workspace."""
    state = _state(ctx)
    root = state.root()
    with _open(state) as (_config, store, runtime):
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
    root = state.root()
    with _open(state) as (config, store, runtime):
        service = WorkspaceService(store, runtime, config)
        workspace = service.require(root)
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
            sessions = service.list(workspace)
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
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n", help="Session name, unique in this workspace.")],
    cwd: Annotated[str, typer.Option("--cwd", help="Working directory for the worker.")],
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent id; defaults to config.")] = None,
    mode: Annotated[Optional[str], typer.Option("--mode", "-m", help="shared or worktree.")] = None,
    base_ref: Annotated[
        Optional[str], typer.Option("--base-ref", help="Git ref to branch from for --mode worktree.")
    ] = None,
) -> None:
    """Create a worker session."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        result = SessionService(store, runtime, config).create(
            workspace, CreateRequest(name=name, agent=agent, cwd=cwd, mode=mode, base_ref=base_ref)
        )
        session = result.session
        payload = {
            "session": session.to_dict(),
            "terminal": result.terminal.address,
            "native_session_id": result.native_session_id,
        }
        state.emit(
            payload,
            lambda: typer.echo(
                f"{session.id}\n"
                f"  name:     {session.name}\n"
                f"  agent:    {session.agent}\n"
                f"  cwd:      {session.cwd}\n"
                f"  mode:     {session.mode}\n"
                f"  terminal: {result.terminal.address}"
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


# --------------------------------------------------------------------- messaging


@app.command()
def send(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    message: Annotated[str, typer.Argument(help="Text to send, or - to read stdin.")],
    force: Annotated[
        bool, typer.Option("--force", help="Type even if the agent is blocked (answers its dialog).")
    ] = False,
) -> None:
    """Send a message to a session."""
    state = _state(ctx)
    text = sys.stdin.read() if message == "-" else message
    if not text.strip():
        raise UsageError("refusing to send an empty message")
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        report = MessagingService(runtime, service.lifecycle).send(target, text, force=force)
        service.lifecycle.refresh(target)
        service.sync_native_id(target)
        payload = report.to_dict()
        payload["status"] = payload["status"]
        state.emit(payload, lambda: typer.echo(f"sent to {report.session} at turn {report.turn}"))


@app.command()
def read(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    lines: Annotated[int, typer.Option("--lines", "-l", help="History lines above the screen.")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing while it changes.")] = False,
) -> None:
    """Print a session's screen and recent history."""
    state = _state(ctx)
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
                    {"text": output.text, "complete": output.complete, "how": output.how, "why": output.why},
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
) -> None:
    """Show one session's or the whole workspace's status."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        if session:
            target = service.get(workspace, session)
            service.lifecycle.refresh(target)
            state.emit(
                {"session": target.to_dict()},
                lambda: typer.echo(
                    f"{target.name}: {target.status}"
                    + (f" ({target.last_error})" if target.last_error else "")
                ),
            )
        else:
            sessions = service.list(workspace)
            state.emit(
                {"workspace_id": workspace.id, "sessions": [s.to_dict() for s in sessions]},
                lambda: _print_sessions(sessions),
            )


@app.command()
def wait(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session name or id.")],
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds before a check-in (not failure).")] = 180.0,
) -> None:
    """Wait until the session stops working."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)
        outcome = MessagingService(runtime, service.lifecycle).wait(target, timeout=timeout)
        service.lifecycle.refresh(target)
        service.sync_native_id(target)
        payload = {
            "session": target.name,
            "ended": outcome.ended,
            "reason": outcome.reason,
            "status": outcome.status,
            "exit_code": outcome.exit_code,
            "turn": outcome.turn,
            "session_status": target.status,
        }
        state.emit(payload, lambda: _print_wait(payload))


def _print_wait(payload: dict[str, Any]) -> None:
    if payload["ended"]:
        typer.echo(f"{payload['session']} stopped: {payload['status'] or payload['reason']}")
    else:
        typer.echo(f"{payload['session']} still {payload['status'] or 'working'} (check-in)")


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


# ------------------------------------------------------------------------ agents


@agents_app.command("list")
def agents_list(ctx: typer.Context) -> None:
    """Show configured agents and what this machine supports."""
    state = _state(ctx)
    config = _config_or_default()
    rows = []
    for spec in list_specs(config):
        definition = _definition(spec.id)
        rows.append(
            {
                "id": spec.id,
                "command": spec.command,
                "enabled": spec.enabled,
                "installed": installed(definition) if definition else False,
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
    typer.echo(f"{'id':<12}{'enabled':<9}{'installed':<11}{'words':<8}{'resume':<8}command")
    for row in rows:
        typer.echo(
            f"{row['id']:<12}{str(row['enabled']).lower():<9}{str(row['installed']).lower():<11}"
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
    payload = {
        "id": agent_id,
        "configured": entry is not None,
        "enabled": entry.enabled if entry else None,
        "command": entry.command if entry else (definition.command if definition else None),
        "installed": installed(definition) if definition else False,
        "version": agent_version(definition) if definition else None,
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
        typer.echo(jsonlib.dumps(config.model_dump(mode="json"), indent=2))
    else:
        typer.echo(dumps(config), nl=False)


def _must_load_config() -> Config:
    try:
        return load_config()
    except FileNotFoundError as exc:
        raise SetupRequired("agentctl is not set up on this machine", "run `agentctl setup` once") from exc


# ----------------------------------------------------------------------- version


@app.command()
def version() -> None:
    """Print the agentctl version."""
    typer.echo(f"agentctl {__version__}")


# -------------------------------------------------------------------------- main


def main() -> None:
    argv = sys.argv[1:]
    if "--json" in argv:
        # Accept `--json` after the subcommand too: Typer only parses callback
        # options before the command name, so hoist it and remember the choice
        # for `--json`-agnostic helpers.
        cleaned = [arg for arg in argv if arg != "--json"]
        sys.argv = [sys.argv[0], "--json", *cleaned]
        os.environ["AGENTCTL_JSON"] = "1"
    try:
        app()
    except AgentctlError as exc:
        _print_error(exc)


def _print_error(exc: AgentctlError) -> None:
    if os.environ.get("AGENTCTL_JSON") == "1" or "--json" in sys.argv:
        typer.echo(jsonlib.dumps(exc.to_dict(), indent=2), err=True)
    else:
        typer.echo(f"error: {exc.message}", err=True)
        if exc.hint:
            typer.echo(f"hint:  {exc.hint}", err=True)
    sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
