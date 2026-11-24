"""Herdr Core adapter: the only place that knows the `hc` CLI.

Verified against hc 0.1.18 (see docs/runtime-capabilities.md). Every call:
- passes `--json` so nothing depends on the human TUI,
- runs with a hard timeout,
- maps HC's error envelope `{"error": {kind, message, hint}}` to agentctl
  errors and preserves the runtime's own error word.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from agentctl.runtime.base import (
    ReadOutput,
    RuntimeAdapter,
    TerminalInfo,
    WaitOutcome,
)
from agentctl.errors import (
    RuntimeBlocked,
    RuntimeFailure,
    RuntimeFinished,
    RuntimeNotFound,
    RuntimeSpawnFailed,
    RuntimeUnavailable,
)

# HC exit codes, from the hc manual and observed behaviour.
_EXIT_NOT_FOUND = 3
_EXIT_NEEDS_TARGET = 7
_EXIT_BLOCKED = 9
_EXIT_STATUS_MISMATCH = 10
_EXIT_FINISHED = 11

_TERMINAL_NOT_FOUND_KINDS = {"not_found"}


class HerdrCoreAdapter(RuntimeAdapter):
    def __init__(self, command: str = "hc", *, timeout: float = 30.0) -> None:
        self.command = command
        self.timeout = timeout

    # ---------------------------------------------------------------- plumbing

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # agentctl decides parentage explicitly (`--no-parent`); an inherited
        # HC_TERMINAL_ID must not silently bind workers to the Leader terminal.
        env.pop("HC_TERMINAL_ID", None)
        return env

    def _run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        executable = shutil.which(self.command)
        if executable is None:
            raise RuntimeUnavailable(
                f"`{self.command}` is not installed or not on PATH",
                "install Herdr Core and re-run `agentctl setup`",
            )
        try:
            proc = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure(
                f"`hc {args[0]}` timed out after {timeout or self.timeout:g}s",
                "raise runtime.call_timeout_seconds or inspect the HC core",
                kind="timeout",
            ) from exc
        except FileNotFoundError as exc:  # pragma: no cover - which() covers it
            raise RuntimeUnavailable(f"`{self.command}` could not be executed") from exc
        if check and proc.returncode != 0:
            raise self._map_error(proc)
        return proc

    def _run_json(self, args: list[str], *, timeout: float | None = None) -> Any:
        proc = self._run(args, timeout=timeout)
        return _parse_json(proc.stdout)

    def _map_error(self, proc: subprocess.CompletedProcess[str]) -> RuntimeFailure:
        body = _parse_json(proc.stderr, quiet=True) or {}
        error = body.get("error") if isinstance(body, dict) else None
        kind = error.get("kind") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        hint = error.get("hint") if isinstance(error, dict) else None
        text = message or (proc.stderr.strip() or f"hc exited {proc.returncode}")
        code = proc.returncode
        if kind in _TERMINAL_NOT_FOUND_KINDS or code == _EXIT_NOT_FOUND:
            return RuntimeNotFound(text, hint, kind=kind, kind_hint=hint)
        if kind == "blocked" or code == _EXIT_BLOCKED:
            return RuntimeBlocked(text, hint, kind=kind, kind_hint=hint)
        if kind == "finished" or code == _EXIT_FINISHED:
            return RuntimeFinished(text, hint, kind=kind, kind_hint=hint)
        if kind == "spawn_failed":
            return RuntimeSpawnFailed(text, hint, kind=kind, kind_hint=hint)
        return RuntimeFailure(text, hint, kind=kind, kind_hint=hint)

    # -------------------------------------------------------------- inspector

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def version(self) -> str:
        proc = self._run(["--version"])
        return proc.stdout.strip().removeprefix("hc ").strip()

    # ------------------------------------------------------------- lifecycle

    def create_terminal(
        self,
        argv: list[str],
        *,
        cwd: str,
        name: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,  # accepted for API symmetry; hc has no --env
    ) -> TerminalInfo:
        if env:
            raise RuntimeFailure("hc cannot inject environment variables into a terminal")
        args = ["run", "--bg", "--json", "--no-parent", "--name", name, "--cwd", cwd]
        for key, value in (labels or {}).items():
            args.extend(["--label", f"{key}={value}"])
        args.append("--")
        args.extend(argv)
        return _terminal(self._run_json(args))

    def send_input(self, terminal: str, text: str, *, force: bool = False) -> dict[str, Any]:
        args = ["send", "--json"]
        if force:
            args.append("--force")
        args.append("--")
        args.append(terminal)
        args.append(text)
        return self._run_json(args)

    def send_keys(self, terminal: str, keys: list[str]) -> dict[str, Any]:
        return self._run_json(["keys", "--json", terminal, *keys])

    def interrupt(self, terminal: str, *, timeout: float | None = None) -> dict[str, Any]:
        args = ["interrupt", "--json"]
        if timeout is not None:
            args.extend(["--timeout", f"{timeout:g}"])
        args.append(terminal)
        return self._run_json(args)

    def read_output(self, terminal: str, *, lines: int = 0) -> ReadOutput:
        body = self._run_json(["read", "--json", "--lines", str(lines), terminal])
        return ReadOutput(
            text=body.get("text", ""),
            complete=bool(body.get("complete", False)),
            how=body.get("how", "screen"),
            why=body.get("why"),
        )

    def get_terminal(self, terminal: str) -> TerminalInfo:
        return _terminal(self._run_json(["get", "--json", terminal]))

    def find_terminal(self, terminal: str) -> TerminalInfo | None:
        try:
            return self.get_terminal(terminal)
        except RuntimeNotFound:
            return None

    def list_terminals(
        self,
        *,
        labels: dict[str, str] | None = None,
        include_finished: bool = False,
    ) -> list[TerminalInfo]:
        args = ["ls", "--json"]
        if include_finished:
            args.append("--all")
        for key, value in (labels or {}).items():
            args.extend(["--label", f"{key}={value}"])
        body = self._run_json(args)
        return [_terminal(item) for item in body.get("terminals", [])]

    def wait(self, terminal: str, *, timeout: float | None = None) -> WaitOutcome:
        args = ["wait", "--json", terminal]
        if timeout is not None:
            args.extend(["--timeout", f"{timeout:g}"])
        body = self._run_json(args, timeout=(timeout or 180) + 30)
        return WaitOutcome(
            ended=bool(body.get("ended")),
            reason=body.get("reason", "unknown"),
            status=body.get("status"),
            exit_code=body.get("exit_code"),
            turn=int(body.get("turn") or 0),
            raw=body,
        )

    def kill(self, terminal: str) -> TerminalInfo:
        return _terminal(self._run_json(["kill", "--json", terminal]))

    def remove(self, terminal: str) -> None:
        try:
            self._run(["rm", "--json", terminal])
        except RuntimeNotFound:
            return


def _terminal(body: dict[str, Any]) -> TerminalInfo:
    status = body.get("status")
    # `ls` reports the status word as a string; `get` reports an object.
    status_word = status.get("state") if isinstance(status, dict) else status
    app = status.get("app") if isinstance(status, dict) else None
    return TerminalInfo(
        address=body.get("address", ""),
        name=body.get("name"),
        state=body.get("state", "unknown"),
        status=status_word,
        app=app,
        cwd=body.get("cwd", ""),
        exit_code=body.get("exit_code"),
        reason=body.get("reason"),
        labels=dict(body.get("labels") or {}),
        reported=bool(body.get("reported", False)),
        raw=body,
    )


def _parse_json(text: str, *, quiet: bool = False) -> Any:
    text = text.strip()
    if not text:
        return None
    # `hc ls --json` and `hc connect` emit one JSON document per line; take the
    # first complete document for single-shot commands.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    if quiet:
        return None
    raise RuntimeFailure("hc returned output that is not JSON", "check `hc --version` compatibility")
