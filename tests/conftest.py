"""Shared fixtures: isolated AGENTCTL_HOME, a fake HC, and a temp workspace."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agentctl-home"
    home.mkdir()
    monkeypatch.setenv("AGENTCTL_HOME", str(home))
    monkeypatch.delenv("AGENTCTL_WORKSPACE", raising=False)
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    return home


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "backend").mkdir()
    return root


@pytest.fixture()
def fake_hc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in `hc` that records calls and answers with canned JSON.

    State lives in $FAKE_HC_STATE so tests can assert on what was invoked and
    simulate finished/blocked terminals without a real core.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "fake-hc-state.json"
    script = bin_dir / "hc"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

state_path = Path({str(state_file)!r})
state = json.loads(state_path.read_text()) if state_path.exists() else {{"calls": [], "terminals": {{}}}}
args = sys.argv[1:]
state["calls"].append(args)
state_path.write_text(json.dumps(state))

def emit(obj, code=0):
    print(json.dumps(obj))
    raise SystemExit(code)

def find(term):
    for name, body in list(state["terminals"].items()):
        if name == term or body["address"] == term or body["name"] == term:
            return body
    return None

cmd = args[0] if args else ""
if cmd == "--version":
    print("hc 9.9.9-test")
    raise SystemExit(0)

if cmd == "run":
    name = args[args.index("--name") + 1]
    assert "--no-parent" in args or "--parent" in args, "run must state its parentage"
    cwd = args[args.index("--cwd") + 1]
    parent = args[args.index("--parent") + 1] if "--parent" in args else None
    argv = args[args.index("--") + 1:]
    labels = {{}}
    for i, a in enumerate(args):
        if a == "--label":
            k, v = args[i + 1].split("=", 1)
            labels[k] = v
    body = {{
        "address": f"t{{len(state['terminals']) + 1}}", "name": name, "cwd": cwd, "argv": argv,
        "state": "live", "status": {{"state": "idle", "app": argv[0]}},
        "labels": labels, "exit_code": None, "reason": None, "reported": True,
        "parent": parent,
        "die_with_parent": not ("--outlive-parent" in args),
    }}
    address = body["address"]
    state["terminals"][name] = body
    state_path.write_text(json.dumps(state))
    emit({{"address": address, "name": name, "cwd": cwd, "state": "live", "agent": argv[0], "turn": 0}})

if cmd == "ls":
    label_filters = {{}}
    for i, a in enumerate(args):
        if a == "--label":
            k, v = args[i + 1].split("=", 1)
            label_filters[k] = v
    out = []
    for body in state["terminals"].values():
        if all(body["labels"].get(k) == v for k, v in label_filters.items()):
            # hc 0.1.22 `ls` omits `reported`; only `get` has the status record.
            item = {{k: v for k, v in body.items() if k != "reported"}}
            item["status"] = (body["status"] or {{}}).get("state")
            out.append(item)
    emit({{"terminals": out, "hidden_finished": 0, "machines": []}})

if cmd in ("get", "kill", "rm"):
    term = args[-1]
    body = find(term)
    if body is None:
        print(json.dumps({{"error": {{"kind": "not_found", "message": f"no terminal {{term}}", "hint": "see hc ls"}}}}), file=sys.stderr)
        raise SystemExit(3)
    if cmd == "rm":
        state["terminals"] = {{k: v for k, v in state["terminals"].items() if v is not body}}
        state_path.write_text(json.dumps(state))
        emit({{"removed": body["address"]}})
    if cmd == "kill":
        body["state"] = "finished"
        body["reason"] = "killed"
        state_path.write_text(json.dumps(state))
    emit(body)

if cmd == "send":
    term = args[args.index("--") + 1]
    text = args[args.index("--") + 2]
    body = find(term)
    if body is None:
        emit({{"error": {{"kind": "not_found", "message": "nope"}}}}, 1)
    state.setdefault("sent", []).append({{"terminal": term, "text": text}})
    state_path.write_text(json.dumps(state))
    body = {{"started": state.get("send_started", True), "status": "idle", "turn": 1}}
    if "--wait" in args:
        body["wait"] = {{"ended": True, "reason": "status", "status": "done",
                         "turn": 2, "exit_code": None}}
    emit(body)

if cmd == "label":
    term = args[1] if args[1] != "--json" else args[2]
    body = find(term)
    if body is None:
        print(json.dumps({{"error": {{"kind": "not_found", "message": f"no terminal {{term}}"}}}}), file=sys.stderr)
        raise SystemExit(3)
    pairs = args[2:] if args[1] != "--json" else args[3:]
    for pair in pairs:
        k, _, v = pair.partition("=")
        if v:
            body["labels"][k] = v
        else:
            body["labels"].pop(k, None)
    state_path.write_text(json.dumps(state))
    emit(body)

if cmd == "keys":
    term = args[2]
    body = find(term)
    if body is None:
        print(json.dumps({{"error": {{"kind": "not_found", "message": f"no terminal {{term}}", "hint": "see hc ls"}}}}), file=sys.stderr)
        raise SystemExit(3)
    emit({{"status": "idle", "turn": 1}})

if cmd == "interrupt":
    term = args[-1]
    body = find(term)
    if body is None:
        print(json.dumps({{"error": {{"kind": "not_found", "message": f"no terminal {{term}}", "hint": "see hc ls"}}}}), file=sys.stderr)
        raise SystemExit(3)
    emit({{"key": "ctrl+c", "outcome": "interrupted"}})

if cmd == "read":
    term = args[-1]
    emit({{"text": "screen for " + term, "complete": True, "how": "screen", "why": None}})

if cmd == "wait":
    if "--children" in args:
        mode = args[args.index("--children") + 1]
        parent_args = args[args.index("--children") + 2:]
        parent = parent_args[0] if parent_args and not parent_args[0].startswith("-") else None
        children = {{}}
        for body in state["terminals"].values():
            if parent is None or body.get("parent") == parent:
                children[body["address"]] = {{
                    "terminal": body["address"],
                    "ended": False,
                    "reason": "running",
                    "status": (body.get("status") or {{}}).get("state"),
                    "exit_code": None,
                    "turn": body.get("turn", 0),
                    "children": [],
                }}
        emit({{"terminal": parent, "ended": True, "reason": "children", "status": None,
               "exit_code": None, "turn": 0, "children": list(children.values())}})
    emit({{"terminal": args[-1], "ended": True, "reason": "status", "status": "done", "exit_code": None, "turn": 1, "children": []}})

if cmd == "server":
    emit({{"version": "9.9.9-test", "live": 0, "finished": 0}})

print(json.dumps({{"error": {{"kind": "invalid_params", "message": "unhandled " + cmd}}}}), file=sys.stderr)
raise SystemExit(6)
"""
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return state_file


def read_state(state_file: Path) -> dict:
    return json.loads(state_file.read_text())


def calls(state_file: Path) -> list[list[str]]:
    return read_state(state_file)["calls"]
