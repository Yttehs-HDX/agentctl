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
from typing import Annotated, Any, Iterator, Optional

import typer

from agentctl import JSON_SCHEMA, __version__
from agentctl.agents.catalog import list_specs, resolve, version_verified
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
from agentctl.clock import now_precise
from agentctl.events import EventBus
from agentctl.leader.service import (
    DEFAULT_LEASE_SECONDS,
    LeaderService,
    validate_instance_id,
)
from agentctl.paths import config_path
from agentctl.runtime.herdr_core import HerdrCoreAdapter
from agentctl.session.messaging import MessagingService
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

hook_app = typer.Typer(no_args_is_help=True, help="Harness hook entry points (called by agents).")
leader_app = typer.Typer(no_args_is_help=True, help="Leader lease: attach, renew, inspect.")
messages_app = typer.Typer(no_args_is_help=True, help="The durable message registry.")
events_app = typer.Typer(no_args_is_help=True, help="The event log.")
inbox_app = typer.Typer(no_args_is_help=True, help="Events waiting for this Leader.")
daemon_app = typer.Typer(no_args_is_help=True, help="The local agentctld service.")
integrations_app = typer.Typer(no_args_is_help=True, help="Harness hook installation.")
app.add_typer(hook_app, name="hook", hidden=True)
app.add_typer(leader_app, name="leader")
app.add_typer(messages_app, name="messages")
app.add_typer(events_app, name="events")
app.add_typer(inbox_app, name="inbox")
app.add_typer(daemon_app, name="daemon")
app.add_typer(integrations_app, name="integrations")

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
    lease_token: str | None = None
    leader_instance: str | None = None

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
    lease_token: Annotated[
        Optional[str],
        typer.Option("--lease-token", help="Leader lease token (or AGENTCTL_LEASE_TOKEN)."),
    ] = None,
    leader_instance: Annotated[
        Optional[str],
        typer.Option("--leader-instance", help="Leader instance id (or AGENTCTL_LEADER_INSTANCE)."),
    ] = None,
) -> None:
    # `--workspace` is kept separate from AGENTCTL_WORKSPACE: discovery needs to
    # tell an explicit flag from an environment default for its `source` field,
    # and the flag must keep winning.
    state = State(
        workspace_arg=workspace or None,
        json=_json_requested(json_output),
        lease_token=lease_token,
        leader_instance=leader_instance,
    )
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
        yield config, store, state.runtime()


def _workspace(state: State, store: Store):
    """Resolve this command's workspace (flag > env > cwd > nearest parent).

    Hook observations are drained first: a worker may have reported while no
    command was running, and every reader below must see that report.
    """
    choice = discover(store, explicit=state.workspace_arg, env=state.env_workspace())
    _ingest(store, choice.workspace)
    return choice.workspace


def _ingest(store: Store, workspace) -> None:
    from agentctl.spool import drain

    try:
        drain(store, workspace)
    except Exception:  # noqa: BLE001 - ingest must never break a command
        pass


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
        row_version = agent_version(definition) if is_installed else None
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
        if row["version_verified"] is False:
            status += " (unverified)"
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
        workspace = _workspace(state, store)
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
) -> None:
    """Create a worker session."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        result = SessionService(store, runtime, config).create(
            workspace,
            CreateRequest(name=name, agent=agent, cwd=cwd, mode=mode, base_ref=base_ref),
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
        _authorize(state, store, workspace)
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
        _authorize(state, store, workspace)
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
    for error in result.errors:
        typer.echo(f"kept: {error}")


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
            help="Type immediately even if no ACK can be expected (records delivery_unknown).",
        ),
    ] = False,
    wait: Annotated[
        Optional[float],
        typer.Option(
            "--wait",
            help="Block until the message's turn completes, up to this many seconds.",
        ),
    ] = None,
    sync: Annotated[
        bool,
        typer.Option(
            "--sync",
            "--legacy",
            help="v0.1 behaviour: type now and wait for a status report instead of queueing.",
        ),
    ] = False,
) -> None:
    """Queue a message for a session and return a message id at once.

    With `--wait` the command becomes a barrier: it returns when the worker's
    turn for this message ends, or when the timeout expires (`ended: false`).
    """
    state = _state(ctx)
    text = _read_message_text(message)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        service = SessionService(store, runtime, config)
        target = service.get(workspace, session)

        if sync:
            report = MessagingService(runtime, service.lifecycle).send(target, text, force=force)
            service.lifecycle.refresh(target)
            service.sync_native_id(target)
            payload = {**report.to_dict(), "mode": "sync"}
            state.emit(
                payload,
                lambda: typer.echo(
                    f"sent to {report.session} at turn {report.turn}"
                    + ("" if report.confirmed else " (delivery unconfirmed)")
                ),
            )
            return

        messages = _message_service(store, runtime)
        queued = messages.enqueue(
            workspace, target, text, leader_epoch=_current_epoch(store, workspace)
        )
        outcome = _dispatch_now(store, runtime, workspace, queued.id, force=force)
        payload = {
            "mode": "async",
            "message": outcome.message.to_dict(),
            "confirmation": outcome.confirmation,
            "reason": outcome.reason,
        }
        if wait is not None:
            payload["wait"] = _wait_for_message(store, workspace, outcome.message.id, wait)
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


def _current_epoch(store: Store, workspace) -> int | None:
    lease = store.leases.get(workspace.id)
    return lease.leader_epoch if lease else None


def _dispatch_now(store: Store, runtime, workspace, message_id: str, *, force: bool):
    """Try to dispatch immediately; a refusal is reported, never fatal.

    Async `send` must return fast even when the worker is busy, because the
    daemon will dispatch later. Only a refusal that proves nothing was typed is
    reported as a queued message with a reason.
    """
    from agentctl.errors import AgentctlError
    from agentctl.messaging.service import DispatchOutcome

    messages = _message_service(store, runtime)
    try:
        return messages.dispatch(workspace, message_id, allow_unconfirmed=force)
    except AgentctlError as exc:
        return DispatchOutcome(
            message=messages.get(workspace, message_id),
            confirmation="queued",
            reason=f"{exc.message} ({exc.hint})" if exc.hint else exc.message,
        )


def _wait_for_message(store: Store, workspace, message_id: str, timeout: float) -> dict[str, object]:
    """Poll one message row until it is terminal or the timeout expires."""
    import time as _time

    deadline = _time.monotonic() + max(0.0, timeout)
    seen_confirmation = None
    last: dict[str, object] = {}
    while True:
        message = store.messages.find_in_workspace(workspace.id, message_id)
        if message is None:
            raise UsageError(f"message {message_id} disappeared while waiting")
        event = store.events.find_by_dedupe(
            workspace.id, "turn.completed", f"{message_id}:completed"
        ) or store.events.find_by_dedupe(workspace.id, "turn.failed", f"{message_id}:failed")
        last = {
            "message_id": message.id,
            "state": message.state,
            "ended": message.state in ("completed", "failed", "cancelled"),
            "result": (event.payload.get("result") if event else None),
        }
        if last["ended"] or _time.monotonic() >= deadline:
            return {**last, "timed_out": not last["ended"]}
        _time.sleep(0.2)


def _print_send(payload: dict[str, Any]) -> None:
    message = payload["message"]
    typer.echo(f"{message['id']}  {message['state']}  ({payload['confirmation']})")
    if payload.get("reason"):
        typer.echo(f"  {payload['reason']}")
    wait = payload.get("wait")
    if wait:
        if wait["ended"]:
            typer.echo(f"  turn ended: {wait['state']}")
        else:
            typer.echo(f"  still {wait['state']} (check-in, not a failure)")


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
        _authorize(state, store, workspace)
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


# ------------------------------------------------------------------- v0.2 CLI

LEASE_TOKEN_ENV = "AGENTCTL_LEASE_TOKEN"
INSTANCE_ENV = "AGENTCTL_LEADER_INSTANCE"


def _env_workspace_id() -> str | None:
    return os.environ.get("AGENTCTL_WORKSPACE_ID") or None


def _env_session_id() -> str | None:
    return os.environ.get("AGENTCTL_SESSION_ID") or None


def _leader(state: State, store: Store):
    return LeaderService(store)


def _authorize(state: State, store: Store, workspace) -> str:
    """Fencing gate for mutating commands.

    Once any Leader has attached to a workspace, a mutating command must present
    that lease's token; a superseded instance is refused. Before anyone attaches
    the workspace is unclaimed and single-operator use works unchanged.
    """
    token = state.lease_token or os.environ.get(LEASE_TOKEN_ENV)
    instance = state.leader_instance or os.environ.get(INSTANCE_ENV)
    return LeaderService(store).authorize(workspace, token, instance_id=instance)


# ---------------------------------------------------------------- hook entry


@hook_app.command("claude-hook")
def hook_claude(ctx: typer.Context) -> None:
    """Handle one Claude Code hook event read from stdin."""
    raw = sys.stdin.read()
    try:
        payload = jsonlib.loads(raw) if raw.strip() else {}
    except jsonlib.JSONDecodeError:
        raise UsageError("hook stdin is not JSON") from None
    with _open(ctx.obj) as (_config, store, _runtime):
        from agentctl.integrations.hooks import claude_hook

        result = claude_hook(
            store,
            payload,
            env_workspace=_env_workspace_id(),
            env_session=_env_session_id(),
        )
    sys.stdout.write(jsonlib.dumps(result) + "\n")
    sys.stdout.flush()


@hook_app.command("session-started")
def hook_session_started(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace id from the environment.")],
    session: Annotated[str, typer.Option("--session", help="Session id from the environment.")],
) -> None:
    """Record that a worker session's harness is up."""
    with _open(ctx.obj) as (_config, store, _runtime):
        ws = _hook_workspace(store, workspace)
        from agentctl.integrations.hooks import record_session_started

        record_session_started(store, ws, session, agent=None)


@hook_app.command("received")
def hook_received(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace")],
    session: Annotated[str, typer.Option("--session")],
    message: Annotated[str, typer.Option("--message", help="Message id read from the text.")],
    source: Annotated[Optional[str], typer.Option("--source")] = None,
) -> None:
    """Record the exact ACK for one message the harness received."""
    with _open(ctx.obj) as (_config, store, _runtime):
        ws = _hook_workspace(store, workspace)
        from agentctl.integrations.hooks import record_message_received

        result = record_message_received(
            store, ws, session, message_id=message, source=source or "hook"
        )
    typer.echo(jsonlib.dumps(result))


@hook_app.command("turn-completed")
def hook_turn_completed(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace")],
    session: Annotated[str, typer.Option("--session")],
    message: Annotated[Optional[str], typer.Option("--message")] = None,
    source: Annotated[Optional[str], typer.Option("--source")] = None,
    result: Annotated[Optional[str], typer.Option("--result")] = None,
) -> None:
    """Record that a worker's turn ended normally."""
    with _open(ctx.obj) as (_config, store, _runtime):
        ws = _hook_workspace(store, workspace)
        from agentctl.integrations.hooks import record_turn_completed

        body = record_turn_completed(
            store, ws, session, message_id=message, source=source or "hook", result=result
        )
    typer.echo(jsonlib.dumps(body))


@hook_app.command("turn-failed")
def hook_turn_failed(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace")],
    session: Annotated[str, typer.Option("--session")],
    message: Annotated[Optional[str], typer.Option("--message")] = None,
    source: Annotated[Optional[str], typer.Option("--source")] = None,
    reason: Annotated[Optional[str], typer.Option("--reason")] = None,
) -> None:
    """Record that a worker's turn ended badly."""
    with _open(ctx.obj) as (_config, store, _runtime):
        ws = _hook_workspace(store, workspace)
        from agentctl.integrations.hooks import record_turn_failed

        body = record_turn_failed(
            store, ws, session, message_id=message, source=source or "hook", reason=reason
        )
    typer.echo(jsonlib.dumps(body))


@hook_app.command("pending")
def hook_pending(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace")],
) -> None:
    """Print the one-line notice a worker should hand its Leader, if any."""
    with _open(ctx.obj) as (_config, store, _runtime):
        ws = _hook_workspace(store, workspace)
        from agentctl.integrations.hooks import pending_notice

        notice = pending_notice(store, ws)
    if notice:
        typer.echo(notice)


def _hook_workspace(store: Store, workspace_id: str):
    from agentctl.integrations.hooks import resolve_workspace

    return resolve_workspace(store, workspace_id)


# -------------------------------------------------------------------- leader


@leader_app.command("attach")
def leader_attach(
    ctx: typer.Context,
    instance: Annotated[Optional[str], typer.Option("--instance", help="Stable id for this Leader instance.")] = None,
    takeover: Annotated[bool, typer.Option("--takeover", help="Replace an expired or abandoned Leader.")] = False,
) -> None:
    """Claim the workspace's Leader lease."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        service = LeaderService(store)
        instance_id = validate_instance_id(instance or os.environ.get(INSTANCE_ENV))
        try:
            if takeover:
                lease = service.takeover(workspace, instance_id=instance_id)
                created = True
            else:
                lease, created = service.attach(workspace, instance_id=instance_id)
        except AgentctlError:
            raise
        bus = EventBus(store)
        bus.emit(
            workspace,
            "leader.attached",
            source="cli",
            leader_epoch=lease.leader_epoch,
            payload={"instance": lease.leader_instance_id},
            dedupe_key=f"attach:{lease.leader_epoch}",
        )
        payload = {"lease": lease.to_dict(), "created": created}
        state.emit(
            payload,
            lambda: typer.echo(
                f"leader attached: epoch {lease.leader_epoch}\n"
                f"  instance:    {lease.leader_instance_id}\n"
                f"  lease token: {lease.lease_token}\n"
                f"  expires:     {lease.expires_at}"
            ),
        )


@leader_app.command("detach")
def leader_detach(ctx: typer.Context) -> None:
    """Release the lease so another Leader may take over at once."""
    state = _state(ctx)
    token = state.lease_token or os.environ.get(LEASE_TOKEN_ENV)
    instance = state.leader_instance or os.environ.get(INSTANCE_ENV)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        service = LeaderService(store)
        service.require(workspace, token, instance_id=instance)
        released = service.release(workspace, token, instance_id=instance or "")
        EventBus(store).emit(
            workspace,
            "leader.detached",
            source="cli",
            dedupe_key=f"detach:{workspace.id}:{token}",
        )
        state.emit({"released": released}, lambda: typer.echo("leader detached"))


@leader_app.command("status")
def leader_status(ctx: typer.Context) -> None:
    """Show who holds the lease and what is waiting for them."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        status = LeaderService(store).status(workspace)
        body = status.to_dict()
        state.emit(
            body,
            lambda: _print_leader_status(body),
        )


def _print_leader_status(body) -> None:
    lease = body.get("lease")
    if not lease:
        typer.echo("no leader has ever attached to this workspace")
    elif body["held"]:
        typer.echo(
            f"leader: epoch {lease['leader_epoch']}, instance {lease['leader_instance_id']}"
        )
        typer.echo(f"  expires in {body['expires_in_seconds']}s")
    else:
        state = "released" if lease.get("released_at") else "expired"
        typer.echo(f"leader: {state} (last epoch {lease['leader_epoch']}, instance {lease['leader_instance_id']})")
    typer.echo(f"  inbox pending: {body['inbox_pending']}")


@leader_app.command("heartbeat")
def leader_heartbeat(ctx: typer.Context) -> None:
    """Renew the lease. Safe to call from a hook on every turn."""
    state = _state(ctx)
    token = state.lease_token or os.environ.get(LEASE_TOKEN_ENV)
    instance = state.leader_instance or os.environ.get(INSTANCE_ENV) or ""
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        lease = LeaderService(store).renew(workspace, token or "", instance_id=instance)
        body = {"lease": lease.to_dict()}
        state.emit(body, lambda: typer.echo(f"renewed until {lease.expires_at}"))


@leader_app.command("takeover")
def leader_takeover(
    ctx: typer.Context,
    instance: Annotated[Optional[str], typer.Option("--instance")] = None,
) -> None:
    """Force this instance to become the Leader, bumping the epoch."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        service = LeaderService(store)
        instance_id = validate_instance_id(instance or os.environ.get(INSTANCE_ENV))
        lease = service.takeover(workspace, instance_id=instance_id)
        EventBus(store).emit(
            workspace,
            "leader.superseded",
            source="cli",
            leader_epoch=lease.leader_epoch,
            payload={"instance": lease.leader_instance_id},
            dedupe_key=f"takeover:{lease.leader_epoch}",
        )
        payload = {"lease": lease.to_dict()}
        state.emit(
            payload,
            lambda: typer.echo(
                f"took over: epoch {lease.leader_epoch}\n"
                f"  instance:    {lease.leader_instance_id}\n"
                f"  lease token: {lease.lease_token}"
            ),
        )


# ------------------------------------------------------------------ messages


def _message_service(store: Store, runtime):
    from agentctl.messaging.service import MessageService
    from agentctl.session.lifecycle import LifecycleManager

    return MessageService(store, runtime, LifecycleManager(store, runtime), bus=EventBus(store))


@messages_app.command("list")
def messages_list(
    ctx: typer.Context,
    state_filter: Annotated[
        Optional[str], typer.Option("--state", help="Filter by message state.")
    ] = None,
    session: Annotated[Optional[str], typer.Option("--session", help="Restrict to one session.")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """List messages in this workspace, newest first."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        session_id = None
        if session:
            session_id = SessionService(store, runtime, config).get(workspace, session).id
        rows = _message_service(store, runtime).list(
            workspace,
            states=[state_filter] if state_filter else None,
            session_id=session_id,
            limit=limit,
        )
        payload = {"workspace_id": workspace.id, "messages": [m.to_dict() for m in rows]}
        state.emit(payload, lambda: _print_messages(rows))


def _print_messages(rows) -> None:
    if not rows:
        typer.echo("no messages")
        return
    typer.echo(f"{'id':<38}{'session':<18}{'state':<18}{'attempts':<9}created")
    for message in rows:
        typer.echo(
            f"{message.id:<38}{message.recipient_id:<18}{message.state:<18}"
            f"{message.attempt_count:<9}{message.created_at}"
        )


@messages_app.command("get")
def messages_get(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Argument(help="Message id.")],
) -> None:
    """Show one message, including what its state means."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        from agentctl.messaging.transitions import describe

        message = _message_service(store, runtime).get(workspace, message_id)
        body = {**message.to_dict(), "state_meaning": describe(message.state)}
        state.emit(
            body,
            lambda: _print_message(body),
        )


def _print_message(body) -> None:
    for key in (
        "id",
        "session_id",
        "recipient_id",
        "state",
        "state_meaning",
        "attempt_count",
        "created_at",
        "dispatched_at",
        "acknowledged_at",
        "completed_at",
        "last_error",
    ):
        typer.echo(f"{key:<18}{body.get(key)}")
    typer.echo(f"{'payload':<18}{body['payload']!r}")


@messages_app.command("retry")
def messages_retry(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Argument(help="Message id.")],
) -> None:
    """Re-queue a message that provably never reached the worker."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        message = _message_service(store, runtime).retry(workspace, message_id)
        state.emit({"message": message.to_dict()}, lambda: typer.echo(f"{message.id}: {message.state}"))


@messages_app.command("cancel")
def messages_cancel(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Argument(help="Message id.")],
) -> None:
    """Cancel a message that has not completed."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        message = _message_service(store, runtime).cancel(workspace, message_id)
        state.emit({"message": message.to_dict()}, lambda: typer.echo(f"{message.id}: {message.state}"))


# -------------------------------------------------------------------- events


@events_app.command("list")
def events_list(
    ctx: typer.Context,
    after: Annotated[Optional[str], typer.Option("--after", help="Only events after this event id.")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    name: Annotated[Optional[str], typer.Option("--event", help="Filter by event name.")] = None,
) -> None:
    """List persisted events, oldest first."""
    state = _state(ctx)
    with _open(state) as (config, store, _runtime):
        workspace = _workspace(state, store)
        rows = EventBus(store).list(
            workspace, after_id=after, limit=limit, names=[name] if name else None
        )
        payload = {"workspace_id": workspace.id, "events": [e.to_dict() for e in rows]}
        state.emit(payload, lambda: _print_events(rows))


def _print_events(rows) -> None:
    if not rows:
        typer.echo("no events")
        return
    for event in rows:
        who = f" session={event.session_id}" if event.session_id else ""
        msg = f" message={event.message_id}" if event.message_id else ""
        typer.echo(f"{event.created_at}  {event.event:<24}{event.id}{who}{msg}")


@events_app.command("watch")
def events_watch(
    ctx: typer.Context,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between polls.")] = 1.0,
) -> None:
    """Print new events as they appear, until interrupted."""
    state = _state(ctx)
    if state.json:
        raise UsageError("--watch cannot be combined with --json")
    import time as _time

    with _open(state) as (config, store, _runtime):
        workspace = _workspace(state, store)
        bus = EventBus(store)
        cursor = bus.store.events.latest_id(workspace.id)
        try:
            while True:
                rows = bus.list(workspace, after_id=cursor, limit=100)
                for event in rows:
                    _print_events([event])
                    cursor = event.id
                _time.sleep(interval)
        except KeyboardInterrupt:
            return


# --------------------------------------------------------------------- inbox


@inbox_app.command("list")
def inbox_list(
    ctx: typer.Context,
    all_items: Annotated[bool, typer.Option("--all", help="Include acked and skipped items.")] = False,
) -> None:
    """Show events waiting for this Leader."""
    state = _state(ctx)
    with _open(state) as (config, store, _runtime):
        workspace = _workspace(state, store)
        bus = EventBus(store)
        if all_items:
            items = store.inbox.list_for_workspace(workspace.id, limit=200)
            for item in items:
                item.event = store.events.find(item.event_id)
        else:
            items = bus.pending_for_leader(workspace, limit=200)
        payload = {"workspace_id": workspace.id, "inbox": [i.to_dict() for i in items]}
        state.emit(payload, lambda: _print_inbox(items))


def _print_inbox(items) -> None:
    if not items:
        typer.echo("inbox empty")
        return
    for item in items:
        event = item.event
        name = event.event if event else "?"
        severity = event.severity if event else "?"
        session = event.session_id if event else None
        typer.echo(f"{item.state:<9}{severity:<9}{name:<24}{item.id}  session={session}")


@inbox_app.command("ack")
def inbox_ack(
    ctx: typer.Context,
    event_id: Annotated[str, typer.Argument(help="Inbox id or event id.")],
) -> None:
    """Mark one inbox item consumed. Idempotent."""
    state = _state(ctx)
    with _open(state) as (config, store, _runtime):
        workspace = _workspace(state, store)
        item = store.inbox.find(event_id)
        if item is None or item.workspace_id != workspace.id:
            from agentctl.errors import EventNotFound

            raise EventNotFound(
                f"no inbox item {event_id!r} in this workspace",
                "run `agentctl inbox list`",
            )
        ok = store.inbox.ack(item.id, stamp=now_precise())
        state.emit({"acked": ok, "inbox_id": item.id}, lambda: typer.echo(f"acked {item.id}"))


# -------------------------------------------------------------------- daemon


@daemon_app.command("start")
def daemon_start(
    ctx: typer.Context,
    foreground: Annotated[bool, typer.Option("--foreground", help="Run in this process.")] = False,
    once: Annotated[bool, typer.Option("--once", help="One pass, then exit (diagnostics).")] = False,
    instance: Annotated[Optional[str], typer.Option("--instance")] = None,
) -> None:
    """Start agentctld for this workspace."""
    from agentctl.daemon.service import (
        Daemon,
        DaemonConfig,
        pid_alive,
        read_state,
        running_state,
        socket_path,
    )

    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        workspace = _workspace(state, store)
        existing = running_state()
        if existing is not None and existing.get("workspace_id") == workspace.id:
            state.emit(
                {"running": True, **existing},
                lambda: typer.echo(f"agentctld already running (pid {existing['pid']})"),
            )
            return
        instance_id = instance or os.environ.get(INSTANCE_ENV) or new_instance_id()
        daemon = Daemon(
            store,
            runtime,
            workspace,
            DaemonConfig(
                workspace_id=workspace.id, instance_id=instance_id, once=once
            ),
        )
        if once:
            daemon.attach_leader()
            try:
                body = daemon.run_once()
            finally:
                daemon.release()
            state.emit(body, lambda: typer.echo(jsonlib.dumps(body, indent=2)))
            return
        if foreground:
            daemon.attach_leader()
            daemon.write_state()
            daemon.install_signal_handlers()
            try:
                daemon.run_forever()
            finally:
                daemon.release()
            return

    # Detached: the child opens its own connection, so this one closes first.
    import subprocess as _subprocess

    log = open(log_path(), "a", encoding="utf-8")
    proc = _subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentctl.cli",
            "daemon",
            "run",
            "--workspace",
            workspace.id,
            "--instance",
            instance_id,
        ],
        stdout=log,
        stderr=log,
        stdin=_subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "AGENTCTL_DAEMON_CHILD": "1"},
    )
    log.close()
    import time as _time

    for _ in range(50):
        info = read_state()
        if info is not None and info.get("pid") == proc.pid:
            state.emit(
                {"running": True, **info},
                lambda: typer.echo(f"agentctld started (pid {proc.pid}, epoch {info.get('epoch')})"),
            )
            return
        _time.sleep(0.1)
    state.emit(
        {"running": False, "pid": proc.pid, "log": str(log_path())},
        lambda: typer.echo(f"agentctld starting (pid {proc.pid}); see {log_path()}"),
    )


@daemon_app.command("run", hidden=True)
def daemon_run(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace id.")],
    instance: Annotated[str, typer.Option("--instance")],
) -> None:
    """Internal: the detached daemon process body."""
    from agentctl.daemon.service import Daemon, DaemonConfig

    with _open(ctx.obj) as (_config, store, runtime):
        found = store.workspaces.find_by_id(workspace)
        if found is None:
            raise UsageError(f"workspace {workspace} is not registered")
        daemon = Daemon(
            store, runtime, found, DaemonConfig(workspace_id=found.id, instance_id=instance)
        )
        daemon.attach_leader()
        daemon.write_state()
        daemon.install_signal_handlers()
        try:
            daemon.run_forever()
        finally:
            daemon.release()


@daemon_app.command("stop")
def daemon_stop(ctx: typer.Context) -> None:
    """Stop agentctld for this workspace."""
    import signal as _signal
    import time as _time

    from agentctl.daemon.service import read_state, running_state

    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        info = running_state()
        if info is None:
            state.emit({"running": False}, lambda: typer.echo("agentctld is not running"))
            return
        pid = int(info["pid"])
        os.kill(pid, _signal.SIGTERM)
        for _ in range(50):
            if not pid_alive(pid):
                break
            _time.sleep(0.1)
        state.emit(
            {"running": False, "stopped_pid": pid},
            lambda: typer.echo(f"stopped agentctld (pid {pid})"),
        )


@daemon_app.command("status")
def daemon_status(ctx: typer.Context) -> None:
    """Show whether agentctld is running for this workspace."""
    from agentctl.daemon.service import log_path, read_state, running_state, socket_path

    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        info = running_state()
        stale = read_state()
        body = {
            "running": info is not None,
            "workspace_id": workspace.id,
            "daemon": info,
            "stale_state": stale if info is None else None,
            "socket": str(socket_path()),
            "log": str(log_path()),
            "inbox_pending": store.inbox.count_pending(workspace.id),
        }
        state.emit(
            body,
            lambda: typer.echo(
                f"agentctld: {'running pid ' + str(info['pid']) if info else 'not running'}\n"
                f"  inbox pending: {body['inbox_pending']}\n"
                f"  log: {body['log']}"
            ),
        )


def new_instance_id() -> str:
    from agentctl.clock import new_id

    return new_id("inst")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_path():
    from agentctl.daemon.service import log_path as _log_path

    return _log_path()


# -------------------------------------------------------------- integrations


@integrations_app.command("list")
def integrations_list(ctx: typer.Context) -> None:
    """Show every known harness and whether its hook is installed."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        from agentctl.integrations.registry import describe_all

        rows = describe_all(store, workspace)
        state.emit({"integrations": rows}, lambda: _print_integrations(rows))


def _print_integrations(rows) -> None:
    typer.echo(f"{'agent':<10}{'installed':<12}{'verified':<10}{'ack':<6}{'wake':<6}notes")
    for row in rows:
        caps = row["capabilities"]
        typer.echo(
            f"{row['agent']:<10}{str(row['installed']).lower():<12}"
            f"{str(caps['verified']).lower():<10}"
            f"{'yes' if caps['receive_message'] else 'no':<6}"
            f"{'yes' if caps['idle_wake'] else 'no':<6}{caps['notes']}"
        )


@integrations_app.command("inspect")
def integrations_inspect(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent id: omp, pi or claude.")],
) -> None:
    """Show one harness's measured capabilities and installed paths."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        from agentctl.integrations.registry import describe_one

        body = describe_one(store, workspace, agent)
        state.emit(body, lambda: typer.echo(jsonlib.dumps(body, indent=2)))


@integrations_app.command("install")
def integrations_install(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent id: omp, pi or claude.")],
    scope: Annotated[str, typer.Option("--scope", help="user or project.")] = "user",
) -> None:
    """Install the agentctl hook for one harness. Idempotent."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        from agentctl.integrations.registry import install

        result = install(store, workspace, agent, scope=scope)
        state.emit(
            result,
            lambda: typer.echo(
                "\n".join(
                    [
                        f"installed {agent} hook",
                        f"  hook:     {result.get('hook_path')}",
                        f"  settings: {result.get('settings_path') or '(none)'}",
                        f"  backup:   {result.get('backup_path') or '(none)'}",
                    ]
                )
            ),
        )


@integrations_app.command("uninstall")
def integrations_uninstall(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent id: omp, pi or claude.")],
    scope: Annotated[str, typer.Option("--scope", help="user or project.")] = "user",
) -> None:
    """Remove an installed hook and restore the previous settings."""
    state = _state(ctx)
    with _open(state) as (_config, store, _runtime):
        workspace = _workspace(state, store)
        _authorize(state, store, workspace)
        from agentctl.integrations.registry import uninstall

        result = uninstall(store, workspace, agent, scope=scope)
        state.emit(result, lambda: typer.echo(f"uninstalled {agent} hook"))


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check the runtime, the agents, the lease and the daemon in one pass."""
    state = _state(ctx)
    with _open(state) as (config, store, runtime):
        from agentctl.diagnostics import run_doctor

        report = run_doctor(store, runtime, config, explicit=state.workspace_arg, env=state.env_workspace())
        state.emit(report, lambda: _print_doctor(report))


def _print_doctor(report) -> None:
    for check in report["checks"]:
        marker = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[check["status"]]
        typer.echo(f"{marker}  {check['name']:<24}{check['detail']}")
    typer.echo("")
    typer.echo(f"result: {report['result']}")


# ------------------------------------------------------------------------ agents


@agents_app.command("list")
def agents_list(ctx: typer.Context) -> None:
    """Show configured agents and what this machine supports."""
    state = _state(ctx)
    config = _config_or_default()
    rows = []
    for spec in list_specs(config):
        definition = _definition(spec.id)
        is_installed = installed(definition) if definition else False
        row_version = agent_version(definition) if is_installed else None
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
    installed_version = agent_version(definition) if definition else None
    payload = {
        "id": agent_id,
        "configured": entry is not None,
        "enabled": entry.enabled if entry else None,
        "command": entry.command if entry else (definition.command if definition else None),
        "installed": installed(definition) if definition else False,
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
        _print_error(exc)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - one envelope for every failure
        # Click's standalone mode turns an escaping exception into exit 1 with
        # only a message, which breaks the promise that `--json` always yields
        # the documented envelope. Re-raise the ones that carry their own exit
        # status and wrap everything else as a generic error.
        if isinstance(exc, SystemExit):
            raise
        _print_error(AgentctlError(str(exc) or type(exc).__name__))


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
