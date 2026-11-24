"""`agentctl doctor`: one pass over everything a Leader depends on.

Each check answers "would a Leader be able to work?" and reports `ok`, `warn` or
`fail`. A warning is a degraded path that still works; a failure is something
the documented workflow needs and cannot get.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentctl.agents.catalog import list_specs, version_verified
from agentctl.agents.detector import installed, version as agent_version
from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.errors import WorkspaceNotInitialized
from agentctl.integrations.registry import describe_all
from agentctl.workspace.discovery import discover


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def run_doctor(store, runtime, config, *, explicit: str | None, env: str | None) -> dict[str, object]:
    checks: list[dict[str, str]] = []

    # 1. Runtime
    if runtime.available():
        try:
            checks.append(_check("herdr-core", "ok", f"hc {runtime.version()}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("herdr-core", "warn", f"installed but not answering: {exc}"))
    else:
        checks.append(_check("herdr-core", "fail", f"`{config.runtime.hc_command}` is not on PATH"))

    # 2. Workspace
    workspace = None
    try:
        choice = discover(store, explicit=explicit, env=env)
        workspace = choice.workspace
        checks.append(
            _check("workspace", "ok", f"{workspace.id} at {workspace.root_path} (found via {choice.source})")
        )
    except WorkspaceNotInitialized as exc:
        checks.append(_check("workspace", "warn", exc.message))

    # 3. Leader lease
    if workspace is not None:
        lease = store.leases.get(workspace.id)
        if lease is None:
            checks.append(
                _check("leader lease", "warn", "no Leader has attached; async wake needs `agentctl leader attach`")
            )
        else:
            from agentctl.clock import elapsed_since

            age = elapsed_since(lease.expires_at)
            live = lease.held and age is not None and age < 0
            remaining = f"{abs(age):.0f}s left" if age is not None else "unknown"
            checks.append(
                _check(
                    "leader lease",
                    "ok" if live else "warn",
                    f"epoch {lease.leader_epoch}, instance {lease.leader_instance_id}, "
                    f"{'live, ' + remaining if live else 'not held'}",
                )
            )

    # 4. Daemon
    from agentctl.daemon.service import running_state

    daemon = running_state()
    checks.append(
        _check(
            "agentctld",
            "ok" if daemon else "warn",
            f"running pid {daemon['pid']} epoch {daemon.get('epoch')}" if daemon
            else "not running; messages dispatch on `send`, but turn events need the daemon",
        )
    )

    # 5. Agents and their version drift
    for spec in list_specs(config):
        definition = AGENT_DEFINITIONS.get(spec.id)
        if definition is None:
            checks.append(_check(f"agent {spec.id}", "warn", "configured without a verified definition"))
            continue
        if not installed(definition):
            checks.append(_check(f"agent {spec.id}", "warn", f"`{definition.command[0]}` not on PATH"))
            continue
        found = agent_version(definition)
        verified = version_verified(definition, found)
        status = "ok" if verified is not False else "warn"
        detail = found or "version unknown"
        if verified is False:
            detail += f" (capabilities measured on {definition.verified_with}; unverified)"
        checks.append(_check(f"agent {spec.id}", status, detail))

    # 6. Integrations
    if workspace is not None:
        for row in describe_all(store, workspace):
            caps = row["capabilities"]
            if not row["supported"]:
                checks.append(_check(f"hook {row['agent']}", "warn", caps["notes"]))
                continue
            if row["installed"]:
                checks.append(_check(f"hook {row['agent']}", "ok", "installed"))
            else:
                checks.append(
                    _check(
                        f"hook {row['agent']}",
                        "warn",
                        f"not installed; run `agentctl integrations install {row['agent']}`",
                    )
                )

    # 7. Nix safety: a hook path inside the store would be unwritable.
    for row in describe_all(store, workspace) if workspace else []:
        record = row.get("record")
        if record and record.get("hook_path") and str(record["hook_path"]).startswith("/nix/"):
            checks.append(_check("nix safety", "fail", f"hook installed under the Nix store: {record['hook_path']}"))

    result = "ok"
    if any(check["status"] == "fail" for check in checks):
        result = "fail"
    elif any(check["status"] == "warn" for check in checks):
        result = "warn"
    return {
        "result": result,
        "workspace": workspace.id if workspace else None,
        "checks": checks,
    }
