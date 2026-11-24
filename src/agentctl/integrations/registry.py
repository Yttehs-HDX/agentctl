"""Which harness hooks exist, where they go, and how to install them safely.

Install rules, in priority order:

1. Never touch the Nix store or any path under `/nix`. Only user-writable
   locations are used.
2. Never overwrite a file agentctl did not write. An existing hook directory
   entry with our name is ours; anything else is left alone.
3. Back up a settings file before editing it, and edit it as JSON with a
   read-modify-write that preserves every other key.
4. Uninstall restores the backup when the file agentctl wrote is still intact.

Every adapter reports what it can actually do; the registry turns those reports
into one capability matrix instead of each caller guessing.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agentctl.clock import now
from agentctl.errors import AgentctlError, IntegrationError
from agentctl.integrations.base import Capabilities, HarnessAdapter
from agentctl.integrations.claude_hook import script as claude_script
from agentctl.integrations.omp_hook import omp_hook, pi_hook
from agentctl.storage.models import Integration, Workspace

OMP_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
OMP_DEFAULT_AGENT_DIR = "~/.omp/agent"
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
PI_DEFAULT_AGENT_DIR = "~/.pi/agent"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_DEFAULT_DIR = "~/.claude"


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _refuse_nix(path: Path) -> None:
    if str(path).startswith("/nix/"):
        raise IntegrationError(
            f"refusing to write {path}: it lives in the Nix store",
            "agentctl only writes user-writable configuration; point the harness at a home directory",
        )


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    _refuse_nix(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agentctl-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.agentctl-{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


class ExtensionAdapter(HarnessAdapter):
    """Install logic for OMP-style extension files.

    The file is written under agentctl's own directory and handed to the worker
    with `-e` at launch. A copy is also offered to the harness's discovery
    directory when the harness supports it, best-effort and never required.
    """

    agent_dir_env: str = ""
    default_agent_dir: str = ""
    hook_filename: str = "agentctl.ts"
    harness_dir_env: str = ""

    def __init__(self) -> None:
        self.agent = type(self).agent

    def agent_dir(self, *, scope: str = "user", workspace_root: str | None = None) -> Path:
        override = os.environ.get(self.agent_dir_env)
        return _expand(override) if override else _expand(self.default_agent_dir)

    def harness_dir(self, *, scope: str = "user") -> Path | None:
        return None

    def asset_dir(self) -> Path:
        """agentctl-owned location of this harness's hook asset."""
        from agentctl.paths import integrations_dir

        return integrations_dir() / self.agent

    def asset_path(self) -> Path:
        return self.asset_dir() / self.hook_filename

    def install(self, workspace: Workspace, *, scope: str = "user") -> dict[str, object]:
        write_launcher()
        source = self._source()
        target = self.asset_path()
        existed = target.exists()
        previous = target.read_text(encoding="utf-8") if existed else None
        changed = previous != source
        if changed:
            _atomic_write(target, source)
        # Ambient copies are a convenience for humans editing a session by hand.
        ambient = self._ambient_path(workspace, scope)
        ambient_written = False
        if ambient is not None:
            try:
                if not ambient.exists() or ambient.read_text(encoding="utf-8") != source:
                    _atomic_write(ambient, source)
                    ambient_written = True
            except (OSError, IntegrationError):
                ambient = None  # discovery is optional; launch never depends on it
        return {
            "agent": self.agent,
            "hook_path": str(target),
            "ambient_path": str(ambient) if ambient else None,
            "ambient_written": ambient_written,
            "settings_path": None,
            "backup_path": None,
            "changed": changed,
            "launch_args": self.launch_args(None, workspace.id),
            "capabilities": self.capabilities().to_dict(),
        }

    def uninstall(self, workspace: Workspace, *, scope: str = "user") -> dict[str, object]:
        removed = []
        for path in (self.asset_path(), self._ambient_path(workspace, scope)):
            if path is not None and path.exists():
                path.unlink()
                removed.append(str(path))
        return {"agent": self.agent, "removed": removed}

    def installed(self, workspace: Workspace, *, scope: str = "user") -> bool:
        return self.installed_for(workspace.id)

    def installed_for(self, workspace_id: str | None = None) -> bool:
        """Whether the hook asset exists on disk.

        Launch only needs the file, not a relational row, so this stays a plain
        filesystem check and works before a workspace row is even resolved.
        """
        target = self.asset_path()
        return target.exists() and "agentctl" in target.read_text(encoding="utf-8", errors="ignore")

    def launch_args(self, store, workspace_id: str) -> list[str]:
        return ["-e", str(self.asset_path())]

    def _ambient_path(self, workspace: Workspace, scope: str) -> Path | None:
        """Best-effort copy in a location the harness discovers by itself."""
        return None

    def _source(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class OmpAdapter(ExtensionAdapter):
    agent = "omp"
    agent_dir_env = OMP_AGENT_DIR_ENV
    harness_dir_env = OMP_AGENT_DIR_ENV
    default_agent_dir = OMP_DEFAULT_AGENT_DIR

    def _source(self) -> str:
        from agentctl.paths import launcher_path

        return omp_hook(str(launcher_path()))

    def _ambient_path(self, workspace: Workspace, scope: str) -> Path | None:
        if scope == "project":
            return Path(workspace.root_path) / ".omp" / "hooks" / "post" / self.hook_filename
        return self.agent_dir() / "hooks" / "post" / self.hook_filename

    def capabilities(self) -> Capabilities:
        return Capabilities(
            agent="omp",
            receive_message=True,
            idle_wake=True,
            turn_started=True,
            turn_completed=True,
            turn_failed=True,
            verified=True,
            notes="measured on 19.1.0: input, turn_start, agent_settled, sendMessage(triggerTurn)",
        )

    def notify_leader(self, event: dict, *, lease, session) -> bool:
        # The Leader wakes through its own hook reading `agentctl hook pending`
        # on the next settle; the daemon only needs to keep the row pending.
        return False


class PiAdapter(ExtensionAdapter):
    agent = "pi"
    agent_dir_env = PI_AGENT_DIR_ENV
    harness_dir_env = PI_AGENT_DIR_ENV
    default_agent_dir = PI_DEFAULT_AGENT_DIR

    def _source(self) -> str:
        from agentctl.paths import launcher_path

        return pi_hook(str(launcher_path()))

    def _ambient_path(self, workspace: Workspace, scope: str) -> Path | None:
        if scope == "project":
            return Path(workspace.root_path) / ".pi" / "extensions" / self.hook_filename
        return self.agent_dir() / "extensions" / self.hook_filename

    def capabilities(self) -> Capabilities:
        return Capabilities(
            agent="pi",
            receive_message=True,
            idle_wake=True,
            turn_started=True,
            turn_completed=True,
            turn_failed=True,
            verified=True,
            notes="measured on 1.5.2: input, turn_start, agent_settled, sendMessage(triggerTurn)",
        )

    def notify_leader(self, event: dict, *, lease, session) -> bool:
        return False


class ClaudeAdapter(HarnessAdapter):
    agent = "claude"
    harness_dir_env = CLAUDE_CONFIG_DIR_ENV

    def harness_dir(self, *, scope: str = "user") -> Path | None:
        return None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            agent="claude",
            receive_message=True,
            idle_wake=True,
            # No hook fires when the model starts a turn. Claiming otherwise
            # would make `turn.started` a guess.
            turn_started=False,
            turn_completed=True,
            turn_failed=True,
            verified=True,
            notes="measured on 2.1.327: UserPromptSubmit prompt, Stop decision=block wakes the model",
            missing=("turn_started",),
        )

    def hook_script_path(self, *, scope: str = "user") -> Path:
        from agentctl.paths import integrations_dir

        return integrations_dir() / self.agent / "agentctl-hook.sh"

    def settings_fragment_path(self) -> Path:
        from agentctl.paths import integrations_dir

        return integrations_dir() / self.agent / "settings.json"

    def launch_args(self, store, workspace_id: str) -> list[str]:
        return ["--settings", str(self.settings_fragment_path())]

    def install(self, workspace: Workspace, *, scope: str = "user") -> dict[str, object]:
        write_launcher()
        script = self.hook_script_path(scope=scope)
        _refuse_nix(script)
        from agentctl.paths import launcher_path

        _atomic_write(script, claude_script(str(launcher_path())), mode=0o755)

        # A fragment passed with `--settings`, so the user's own settings file is
        # never read, edited or backed up. Claude Code merges what it is given.
        fragment = self.settings_fragment_path()
        command = f'"{script}"'
        hooks = {
            event: [{"hooks": [{"type": "command", "command": command}]}]
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
        }
        _atomic_write(fragment, json.dumps({"hooks": hooks}, indent=2) + "\n")
        return {
            "agent": self.agent,
            "hook_path": str(script),
            "settings_path": str(fragment),
            "backup_path": None,
            "ambient_path": None,
            "changed": True,
            "launch_args": self.launch_args(None, workspace.id),
            "capabilities": self.capabilities().to_dict(),
        }

    def uninstall(self, workspace: Workspace, *, scope: str = "user") -> dict[str, object]:
        removed = []
        for path in (self.hook_script_path(scope=scope), self.settings_fragment_path()):
            if path.exists():
                path.unlink()
                removed.append(str(path))
        return {"agent": self.agent, "removed": removed}

    def installed(self, workspace: Workspace, *, scope: str = "user") -> bool:
        return self.installed_for(workspace.id)

    def installed_for(self, workspace_id: str | None = None) -> bool:
        return self.settings_fragment_path().exists() and self.hook_script_path().exists()

    def notify_leader(self, event: dict, *, lease, session) -> bool:
        # Claude is woken by its own Stop hook reading the inbox; the daemon
        # keeps the event pending so that hook has something to find.
        return False


def _has_command(entries: list, script: str) -> bool:
    for entry in entries:
        command = entry.get("command") if isinstance(entry, dict) else None
        if isinstance(command, str) and script in command:
            return True
    return False


ADAPTERS: dict[str, HarnessAdapter] = {
    "omp": OmpAdapter(),
    "pi": PiAdapter(),
    "claude": ClaudeAdapter(),
}


def adapter_for(agent: str) -> HarnessAdapter:
    adapter = ADAPTERS.get(agent)
    if adapter is None:
        raise AgentctlError(
            f"no agentctl integration exists for agent {agent!r}",
            f"supported agents: {', '.join(sorted(ADAPTERS))}",
        )
    return adapter


def adapter_for_agent(workspace: Workspace, session) -> HarnessAdapter | None:
    """The adapter that would wake a Leader, or None when nothing can."""
    agent = getattr(session, "agent", None)
    if agent:
        return ADAPTERS.get(agent)
    return None


def describe_all(store, workspace: Workspace) -> list[dict[str, object]]:
    rows = []
    for agent, adapter in sorted(ADAPTERS.items()):
        record = store.integrations.get(workspace.id, agent, "user")
        rows.append(
            {
                "agent": agent,
                "installed": adapter.installed(workspace),
                "record": record.to_dict() if record else None,
                "capabilities": adapter.capabilities().to_dict(),
                "supported": True,
            }
        )
    # Agents agentctl can run but has no hook for are listed with the reason,
    # rather than omitted, so `integrations list` answers "what about codex?".
    for agent in ("codex", "opencode"):
        rows.append(
            {
                "agent": agent,
                "installed": False,
                "record": None,
                "capabilities": Capabilities(
                    agent=agent,
                    verified=False,
                    notes="no hook API verified on this machine; sends stay unconfirmed",
                    missing=("receive_message", "idle_wake", "turn_started", "turn_completed"),
                ).to_dict(),
                "supported": False,
            }
        )
    return rows


def describe_one(store, workspace: Workspace, agent: str) -> dict[str, object]:
    if agent in ADAPTERS:
        return describe_all(store, workspace)[
            [row["agent"] for row in describe_all(store, workspace)].index(agent)
        ]
    for row in describe_all(store, workspace):
        if row["agent"] == agent:
            return row
    raise AgentctlError(f"unknown agent {agent!r}", f"supported: {', '.join(sorted(ADAPTERS))}")


def install(store, workspace: Workspace, agent: str, *, scope: str = "user") -> dict[str, object]:
    launcher = write_launcher()
    adapter = adapter_for(agent)
    result = adapter.install(workspace, scope=scope)
    stamp = now()
    config_dir = None
    record = Integration(
        workspace_id=workspace.id,
        agent=agent,
        scope=scope,
        hook_path=str(result.get("hook_path")),
        settings_path=str(result.get("settings_path")) if result.get("settings_path") else None,
        config_dir=str(config_dir) if config_dir else None,
        capability=json.dumps(adapter.capabilities().to_dict()),
        version=None,
        installed_at=stamp,
        updated_at=stamp,
    )
    store.integrations.upsert(record)
    result["launcher"] = str(launcher)
    return result


def launch_args_for(store, workspace_id: str, agent: str) -> list[str]:
    """How a worker of `agent` is told to load its hook.

    Returns [] when the integration is not installed, so a workspace without an
    integration still launches cleanly instead of pointing at a missing file.
    """
    adapter = ADAPTERS.get(agent)
    if adapter is None or not adapter.installed_for(workspace_id):
        return []
    return adapter.launch_args(store, workspace_id)


def uninstall(store, workspace: Workspace, agent: str, *, scope: str = "user") -> dict[str, object]:
    adapter = adapter_for(agent)
    result = adapter.uninstall(workspace, scope=scope)
    store.integrations.delete(workspace.id, agent, scope)
    return result


def write_launcher() -> Path:
    """Write the absolute `agentctl` launcher hooks invoke.

    The shebang names this interpreter, so the launcher works from any PATH and
    from inside a worker whose environment agentctl did not choose. Returns the
    path; content is rewritten each install so an upgraded interpreter is picked
    up (the write is atomic, so an in-flight hook never sees a partial file).
    """
    import sys

    from agentctl.paths import launcher_path

    target = launcher_path()
    # AGENTCTL_HOME is baked in: a hook runs inside somebody else's process tree,
    # where the variable agentctl was installed with is not guaranteed to exist.
    home = str(target.parent.parent)
    content = (
        "#!/bin/sh\n"
        "# agentctl launcher written by `agentctl integrations install`.\n"
        "# The home is fixed, not inherited: a hook must read the registry that\n"
        "# installed it even when its parent process has no agentctl variables.\n"
        f'AGENTCTL_HOME="{home}"\n'
        'export AGENTCTL_HOME\n'
        f'exec "{sys.executable}" -m agentctl.cli "$@"\n'
    )
    _atomic_write(target, content, mode=0o755)
    return target
