"""Session service: create, delete, query, recover.

Contract:
- The registry row exists before any runtime resource; if the runtime step
  fails, the row and any terminal are rolled back so no orphan terminal or
  half-created session is left behind.
- The agentctl session id never depends on a runtime terminal id.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentctl.agents.catalog import definition_for, resolve
from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.config.schema import Config
from agentctl.errors import (
    AgentNotConfigured,
    AgentctlError,
    RuntimeFailure,
    SessionNameConflict,
    SessionNotFound,
    UsageError,
)
from agentctl.paths import session_state_dir
from agentctl.leader.service import ROLE_LABEL, WORKER_ROLE, LeaderService
from agentctl.runtime.base import RuntimeAdapter, TerminalInfo
from agentctl.session.lifecycle import LifecycleManager
from agentctl.session.worktree import create_worktree, ensure_git_repo, remove_worktree
from agentctl.storage.models import Session, Workspace
from agentctl.workspace.resolver import canonical, ensure_inside_workspace, now, respell_under

SESSION_ID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
HC_NAME_MAX = 32
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def is_hc_address_shaped(name: str) -> bool:
    """Whether Herdr would read this name as a terminal address (`t7`, `t1b`).

    Measured on 0.1.18: `--name` rejects a name shaped like an address, because
    the address and the name share one namespace in `hc get`.
    """
    return bool(re.fullmatch(r"t[0-9][0-9a-z]*", name))


def _with_leftovers(hint: str, leftovers: list[str]) -> str:
    if not leftovers:
        return hint
    return f"{hint}; cleanup incomplete: {'; '.join(leftovers)}"


def _directory_size(root: Path) -> int:
    """Bytes of regular-file data, without following symlinks out of the tree."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


@dataclass(slots=True)
class CreateRequest:
    name: str
    agent: str | None
    cwd: str
    mode: str | None = None
    base_ref: str | None = None
    #: Optional first message. Passing it here lets the agent start on its work
    #: without a send that a booting process could drop.
    task: str | None = None


@dataclass(slots=True)
class CreateResult:
    session: Session
    terminal: TerminalInfo
    native_session_id: str | None
    ready: bool = False


@dataclass(slots=True)
class PruneResult:
    sessions: int = 0
    directories: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "sessions": self.sessions,
            "directories": self.directories,
            "bytes_freed": self.bytes_freed,
            "errors": list(self.errors),
            "notes": list(self.notes),
            "dry_run": self.dry_run,
        }


class SessionService:
    def __init__(self, store, runtime: RuntimeAdapter, config: Config) -> None:
        self.store = store
        self.runtime = runtime
        self.config = config
        self.lifecycle = LifecycleManager(store, runtime)

    # -------------------------------------------------------------- selection

    def resolve_agent(self, workspace: Workspace, requested: str | None) -> str:
        """Explicit flag -> workspace default -> global default -> error."""
        if requested:
            resolve(self.config, requested)
            return requested
        if workspace.default_agent:
            resolve(self.config, workspace.default_agent)
            return workspace.default_agent
        if self.config.defaults.agent:
            resolve(self.config, self.config.defaults.agent)
            return self.config.defaults.agent
        raise AgentNotConfigured(
            "no agent selected and no default configured",
            "pass --agent, or run `agentctl config set default-agent <id>`",
        )

    def validate_cwd(self, workspace: Workspace, raw_cwd: str) -> str:
        path = canonical(raw_cwd)
        ensure_inside_workspace(path, workspace.root_path)
        # Store the registered spelling so `rebind`'s relative_to keeps working
        # when the caller typed a case variant of the workspace root.
        return respell_under(path, workspace.root_path)

    def hc_name_for(self, session_id: str, session_name: str) -> str:
        slug = re.sub(r"[^a-z0-9._-]+", "-", session_name.lower()).strip("-._")
        suffix = session_id.removeprefix("sess_")[:6]
        if slug:
            budget = HC_NAME_MAX - len("ac-") - len(suffix) - 1
            slug = slug[:budget].strip("-._") or "s"
            candidate = f"ac-{slug}-{suffix}"
        else:
            candidate = f"ac-{suffix}"
        if not _NAME_OK.match(candidate):
            candidate = f"ac-{suffix}"
        return candidate[:HC_NAME_MAX]

    # ------------------------------------------------------------------ create

    def _leader_terminal(self, workspace: Workspace) -> str | None:
        status = LeaderService(self.store, self.runtime).status(workspace)
        return status.terminal.address if status.terminal and status.live else None

    def create(
        self,
        workspace: Workspace,
        request: CreateRequest,
        *,
        wait_timeout: float = 0.0,
    ) -> CreateResult:
        name = request.name.strip()
        if not name:
            raise UsageError("session name must not be empty")
        if len(name) > 64:
            raise UsageError("session name must be at most 64 characters")
        if is_hc_address_shaped(name):
            raise UsageError(
                f"session name {name!r} is shaped like a Herdr terminal address",
                "Herdr would read it as an address; choose a name like 'worker' or 'reviewer'",
            )
        if self.store.sessions.find_in_workspace(workspace.id, name) is not None:
            raise SessionNameConflict(
                f"session {name!r} already exists in workspace {workspace.id}",
                "choose another --name, or delete the existing session first",
            )

        agent_id = self.resolve_agent(workspace, request.agent)
        definition = definition_for(agent_id)
        mode = request.mode or self.config.session.default_mode
        if mode not in ("shared", "worktree"):
            raise UsageError(f"unknown mode {mode!r}; use shared or worktree")

        cwd = self.validate_cwd(workspace, request.cwd)
        session_id = f"sess_{uuid.uuid4().hex}"
        native_id: str | None = None
        native_dir: str | None = None
        argv = list(self.config.agents[agent_id].command if agent_id in self.config.agents else definition.command)

        if definition.session_id == "explicit" and definition.session_id_flag:
            native_id = str(uuid.uuid4())
            argv.extend([definition.session_id_flag, native_id])
        elif definition.session_id == "session_dir" and definition.session_dir_flag:
            native_dir = str(session_state_dir() / session_id)
            argv.extend([definition.session_dir_flag, native_dir])

        worktree_path: str | None = None
        worktree_branch: str | None = None
        git_root: str | None = None
        if mode == "worktree":
            git_root = ensure_git_repo(cwd)
            worktree_path, worktree_branch = create_worktree(
                workspace_root=workspace.root_path,
                repo_root=git_root,
                session_name=name,
                session_id=session_id,
                base_ref=request.base_ref,
            )
            cwd = worktree_path

        stamp = now()
        session = Session(
            id=session_id,
            workspace_id=workspace.id,
            name=name,
            agent=agent_id,
            cwd=cwd,
            mode=mode,
            hc_terminal_id=None,
            hc_name=self.hc_name_for(session_id, name),
            native_session_id=native_id,
            native_session_dir=native_dir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            git_root=git_root,
            runtime_started_at=stamp,
            status="starting",
            last_error=None,
            created_at=stamp,
            updated_at=stamp,
            deleted_at=None,
        )
        try:
            self.store.sessions.insert(session)
        except sqlite3.IntegrityError as exc:
            # The unique index is the arbiter. The pre-check above only exists
            # to give a clean message; two creates can pass it together.
            leftovers = self._remove_created_resources(
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                git_root=git_root,
                native_dir=native_dir,
            )
            raise SessionNameConflict(
                f"session {name!r} already exists in workspace {workspace.id}",
                _with_leftovers(
                    "choose another --name, or delete the existing session first", leftovers
                ),
            ) from exc

        try:
            if native_dir:
                Path(native_dir).mkdir(parents=True, exist_ok=True)
            terminal = self.runtime.create_terminal(
                with_env(argv, launch_env(workspace, session)),
                cwd=cwd,
                name=session.hc_name or self.hc_name_for(session_id, name),
                labels=worker_labels(workspace, session),
                parent=self._leader_terminal(workspace),
                outlive_parent=True,
                first_task=request.task,
            )
        except BaseException as exc:
            self.store.sessions.hard_delete(session.id)
            leftovers = self._remove_created_resources(
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                git_root=git_root,
                native_dir=native_dir,
            )
            if leftovers:
                note = "cleanup incomplete: " + "; ".join(leftovers)
                if isinstance(exc, AgentctlError):
                    exc.hint = f"{exc.hint} {note}" if exc.hint else note
                else:
                    exc.add_note(note)
            raise

        session.hc_terminal_id = terminal.address
        session.hc_name = terminal.name or session.hc_name
        self.store.sessions.update_runtime(
            session.id,
            hc_terminal_id=session.hc_terminal_id,
            hc_name=session.hc_name,
            native_session_id=session.native_session_id,
            native_session_dir=session.native_session_dir,
            status="starting",
            last_error=None,
            updated_at=now(),
        )
        if definition.session_id == "session_dir" and native_dir and not session.native_session_id:
            captured = self.capture_native_session_id(session)
            if captured:
                session.native_session_id = captured
                self.store.sessions.update_runtime(
                    session.id,
                    hc_terminal_id=session.hc_terminal_id,
                    hc_name=session.hc_name,
                    native_session_id=captured,
                    native_session_dir=native_dir,
                    status="starting",
                    last_error=None,
                    updated_at=now(),
                )
        self.store.workspaces.touch(workspace.id, now())
        return CreateResult(
            session=session,
            terminal=terminal,
            native_session_id=session.native_session_id,
            ready=self._wait_ready(session, wait_timeout),
        )

    def _remove_created_resources(
        self,
        *,
        worktree_path: str | None,
        worktree_branch: str | None,
        git_root: str | None,
        native_dir: str | None,
    ) -> list[str]:
        """Undo what `create` made; return anything it could not undo.

        A cleanup failure is reported, never swallowed: the session row is gone,
        so no later command can discover the leftover.
        """
        leftovers: list[str] = []
        if worktree_path and git_root:
            removed, message = remove_worktree(
                repo_root=git_root, path=worktree_path, branch=worktree_branch
            )
            if not removed:
                leftovers.append(
                    f"worktree {worktree_path} ({message or 'git worktree remove failed'})"
                )
        if native_dir:
            # The row is gone, so this directory can never be resumed or pruned.
            shutil.rmtree(native_dir, ignore_errors=True)
            if Path(native_dir).exists():
                leftovers.append(f"session directory {native_dir}")
        return leftovers

    def _wait_ready(self, session: Session, timeout: float) -> bool:
        """Whether the agent has reported status; optionally wait for it.

        A created terminal is not a usable worker yet. `--wait` lets a Leader
        pay the boot time here instead of discovering it on the first send.
        """
        if timeout > 0:
            try:
                self.lifecycle.wait_until_ready(session, timeout=timeout)
            except RuntimeFailure:
                return False
            # `--wait` promises a usable worker; persist the status it reached
            # so `ready: true` and the row do not disagree.
            self.lifecycle.refresh(session)
        try:
            terminal = self.lifecycle.find_terminal(session)
        except RuntimeFailure:
            return False
        return bool(terminal and terminal.live and terminal.reported)

    # -------------------------------------------------------------- native ids

    def capture_native_session_id(self, session: Session) -> str | None:
        """Read the agent's own session id from its private session directory."""
        if session.native_session_id or not session.native_session_dir:
            return None
        directory = Path(session.native_session_dir)
        if not directory.is_dir():
            return None
        candidates = sorted(
            (p for p in directory.glob("*.jsonl")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            match = SESSION_ID_RE.search(path.name)
            if match:
                return match.group(1)
        return None

    def sync_native_id(self, session: Session) -> str | None:
        captured = self.capture_native_session_id(session)
        if captured:
            self.store.sessions.update_runtime(
                session.id,
                hc_terminal_id=session.hc_terminal_id,
                hc_name=session.hc_name,
                native_session_id=captured,
                native_session_dir=session.native_session_dir,
                status=session.status,
                last_error=session.last_error,
                updated_at=now(),
            )
            session.native_session_id = captured
        return captured

    # --------------------------------------------------------------- retrieval

    def get(self, workspace: Workspace, name_or_id: str) -> Session:
        session = None
        if name_or_id.startswith("sess_"):
            candidate = self.store.sessions.find_by_id(name_or_id)
            if candidate is not None and candidate.workspace_id == workspace.id and candidate.deleted_at is None:
                session = candidate
        if session is None:
            session = self.store.sessions.find_in_workspace(workspace.id, name_or_id)
        if session is None:
            raise SessionNotFound(
                f"no session {name_or_id!r} in this workspace",
                "run `agentctl list` to see this workspace's sessions",
            )
        return session

    def list(self, workspace: Workspace, *, refresh: bool = True) -> list[Session]:
        sessions = self.store.sessions.list_for_workspace(workspace.id)
        if refresh:
            for session in sessions:
                self.lifecycle.refresh(session)
                if not session.native_session_id:
                    self.sync_native_id(session)
        return sessions

    # ------------------------------------------------------------------ delete

    def delete(self, workspace: Workspace, name_or_id: str, *, keep_worktree: bool = False) -> Session:
        session = self.get(workspace, name_or_id)
        terminal = self.lifecycle.find_terminal(session, refuse_address_collision=True)
        if terminal is not None and terminal.live:
            try:
                self.runtime.kill(terminal.address)
            except RuntimeFailure as exc:
                # A terminal that already vanished is not a reason to keep the
                # registry row; anything else is reported and aborts deletion.
                if exc.code != "runtime_not_found":
                    raise
        if terminal is not None:
            self.runtime.remove(terminal.address)
        kept_worktree_note: str | None = None
        if session.worktree_path and session.git_root and not keep_worktree:
            from agentctl.session.worktree import worktree_is_clean

            if worktree_is_clean(session.worktree_path):
                removed, message = remove_worktree(
                    repo_root=session.git_root,
                    path=session.worktree_path,
                    branch=session.worktree_branch,
                )
                if not removed:
                    kept_worktree_note = (
                        f"worktree kept at {session.worktree_path} "
                        f"({message or 'git worktree remove failed'})"
                    )
            else:
                # Uncommitted worker output is project data: keep it and say so.
                kept_worktree_note = (
                    f"worktree kept at {session.worktree_path} (uncommitted changes)"
                )
        self.store.sessions.mark_deleted(session.id, "stopped", now())
        if kept_worktree_note:
            self.store.sessions.set_status(session.id, "stopped", kept_worktree_note, now())
        session.status = "stopped"
        session.last_error = kept_worktree_note
        session.deleted_at = now()
        session.hc_terminal_id = None
        self.store.workspaces.touch(workspace.id, now())
        return session

    # ------------------------------------------------------------------ prune

    def prune(self, workspace: Workspace, *, dry_run: bool = False) -> PruneResult:
        """Reclaim agentctl-owned state for this workspace's soft-deleted rows.

        The deletion target is derived from the session id, never taken from the
        row's `native_session_dir`. A corrupted or tampered row can therefore
        only ever point at its own directory. A recorded path that disagrees is
        reported as a note and otherwise ignored. Worktrees, git branches and
        agent-global stores are project/agent data and stay.
        """
        result = PruneResult(dry_run=dry_run)
        state_dir = Path(session_state_dir()).resolve()
        for session in self.store.sessions.list_deleted(workspace.id):
            target = state_dir / session.id
            # The id must name a direct child, and that child must be a real
            # directory. Resolving a planted symlink would let it redirect the
            # delete to another session's directory.
            if target.parent != state_dir or target.is_symlink():
                result.errors.append(
                    f"session {session.name!r}: refusing unsafe session id {session.id!r}"
                )
                continue
            if session.native_session_dir:
                claimed = Path(session.native_session_dir).resolve()
                if claimed != target.resolve():
                    result.notes.append(
                        f"session {session.name!r}: recorded path {claimed} ignored; "
                        f"removing {target}"
                    )
            if not target.exists():
                if not dry_run:
                    self.store.sessions.hard_delete(session.id)
                result.sessions += 1
                continue
            try:
                size = _directory_size(target)
            except OSError as exc:
                result.errors.append(f"session {session.name!r}: cannot size {target}: {exc}")
                continue
            if dry_run:
                result.directories += 1
                result.bytes_freed += size
                result.sessions += 1
                continue
            try:
                shutil.rmtree(target)
            except OSError as exc:
                result.errors.append(f"session {session.name!r}: cannot remove {target}: {exc}")
                continue
            self.store.sessions.hard_delete(session.id)
            result.sessions += 1
            result.directories += 1
            result.bytes_freed += size
        return result

    # ----------------------------------------------------------------- recover

    def recover(self, workspace: Workspace, name_or_id: str) -> tuple[Session, str]:
        """Re-attach to a session's terminal, or relaunch it from its native id."""
        session = self.get(workspace, name_or_id)
        terminal = self.lifecycle.find_terminal(session)
        if terminal is not None and terminal.live:
            if terminal.address != session.hc_terminal_id:
                self.store.sessions.update_runtime(
                    session.id,
                    hc_terminal_id=terminal.address,
                    hc_name=terminal.name,
                    native_session_id=session.native_session_id,
                    native_session_dir=session.native_session_dir,
                    status=session.status,
                    last_error=None,
                    updated_at=now(),
                )
            self.lifecycle.refresh(session)
            return session, "attached"

        definition = AGENT_DEFINITIONS.get(session.agent)
        native = session.native_session_id or self.sync_native_id(session)
        if definition is None or not definition.supports_resume or not definition.resume_flag or not native:
            self.store.sessions.set_status(
                session.id,
                "unreachable",
                "runtime terminal is gone and no native session is available to resume",
                now(),
            )
            session.status = "unreachable"
            return session, "unrecoverable"

        argv = list(self.config.agents[session.agent].command if session.agent in self.config.agents else definition.command)
        if definition.session_id == "session_dir" and session.native_session_dir:
            argv.extend([definition.session_dir_flag, session.native_session_dir])
        argv.extend([definition.resume_flag, native])
        new_terminal = self.runtime.create_terminal(
            with_env(argv, launch_env(workspace, session)),
            cwd=session.cwd,
            name=self.hc_name_for(session.id, session.name),
            labels=worker_labels(workspace, session),
            parent=self._leader_terminal(workspace),
            outlive_parent=True,
        )
        stamp = now()
        self.store.sessions.update_runtime(
            session.id,
            hc_terminal_id=new_terminal.address,
            hc_name=new_terminal.name,
            native_session_id=native,
            native_session_dir=session.native_session_dir,
            status="starting",
            last_error=None,
            updated_at=stamp,
            runtime_started_at=stamp,
        )
        session.runtime_started_at = stamp
        session.hc_terminal_id = new_terminal.address
        session.hc_name = new_terminal.name
        session.status = "starting"
        # A relaunch is not proof of life: re-derive once so a process that
        # exits immediately is reported as failed instead of "resumed".
        self.lifecycle.refresh(session)
        return session, "resumed"

    def recover_workspace(self, workspace: Workspace) -> list[tuple[Session, str]]:
        results: list[tuple[Session, str]] = []
        for session in self.store.sessions.list_for_workspace(workspace.id):
            results.append(self.recover(workspace, session.id))
        return results



# ---------------------------------------------------------------------------
# v0.2: integration-aware launch environment
# ---------------------------------------------------------------------------


def worker_labels(workspace: Workspace, session: Session) -> dict[str, str]:
    """Labels that let a later Leader find this Worker without a registry scan.

    The role label is also what stops a Worker's terminal being adopted as the
    Leader: `leader attach` refuses anything carrying `agentctl.role=worker`.
    """
    return {
        "agentctl.workspace": workspace.id,
        "agentctl.session": session.id,
        ROLE_LABEL: WORKER_ROLE,
    }


def launch_env(workspace: Workspace, session: Session) -> dict[str, str]:
    """Identity a Worker carries into its own process.

    Herdr has no agentctl concept, so these names are the whole contract: a
    Worker that wants to call agentctl back, or a script that wants to know which
    workspace it belongs to, reads them and nothing else.
    """
    return {
        "AGENTCTL_WORKSPACE_ID": workspace.id,
        "AGENTCTL_SESSION_ID": session.id,
        "AGENTCTL_SESSION_NAME": session.name,
    }


def with_env(argv: list[str], env: dict[str, str]) -> list[str]:
    """Prefix `argv` with `env -u HC_TERMINAL_ID K=V ...` when there is anything to set.

    `hc` starts programs with the core's environment and has no flag to inject
    variables, so `env` in the argv is the only supported route. Values travel
    as separate argv elements, so newlines, quotes and `$` in a session name
    cannot be re-parsed by a shell.
    """
    if not env:
        return list(argv)
    prefix = ["env", "-u", "HC_TERMINAL_ID"]
    for key, value in env.items():
        if value is None:
            continue
        prefix.append(f"{key}={value}")
    return [*prefix, *argv]
