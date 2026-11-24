"""`agentctld`: the long-lived worker behind the CLI.

What it owns
------------
- the Herdr Core event stream, translated into agentctl events;
- the Leader lease, renewed while the daemon lives and released on shutdown;
- message dispatch for queued work, one message per worker at a time;
- delivery of Leader-relevant events, with an explicit backoff instead of a
  busy loop.

What it deliberately does not own
---------------------------------
Harness hooks talk to the CLI, not to the daemon. That keeps a hook's cost at
one short process and means a daemon restart never drops a hook event, because
nothing in flight depends on the daemon being up.

Crash safety
------------
Every unit of work is a row in `outbox` or `inbox`. A restart re-reads those
rows, so "the daemon died" degrades to "the work is still pending". Nothing is
marked done before the side effect that completes it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from agentctl.agents.definitions import AGENT_DEFINITIONS
from agentctl.clock import now, now_precise, plus
from agentctl.events.bus import EventBus
from agentctl.events.ingest import Ingestor, TerminalView
from agentctl.leader.service import DEFAULT_LEASE_SECONDS, LeaderService
from agentctl.messaging.service import MessageService
from agentctl.paths import config_dir
from agentctl.runtime.base import RuntimeAdapter
from agentctl.runtime.herdr_core import event_command, parse_event_line
from agentctl.session.lifecycle import LifecycleManager
from agentctl.storage.models import OutboxItem, Workspace
from agentctl.storage.database import transaction

# How often the daemon looks at the queue and the outbox when nothing wakes it.
TICK_SECONDS = 1.0
# How long an unattended inbox event may wait before it is announced again.
REDELIVER_AFTER_SECONDS = 180.0


def state_path():
    return config_dir() / "daemon.json"


def socket_path():
    return config_dir() / "daemon.sock"


def log_path():
    return config_dir() / "daemon.log"


@dataclass(slots=True)
class DaemonConfig:
    workspace_id: str
    instance_id: str
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    tick: float = TICK_SECONDS
    once: bool = False


class Notifier:
    """Get one event in front of one Leader.

    The mechanism is a harness adapter's `notify_leader`. It must never block
    the daemon: a Leader whose harness cannot be woken keeps the event pending
    in the inbox, and the next attempt is scheduled with a backoff.
    """

    def __init__(self, store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus

    def deliver(self, workspace: Workspace, item) -> str:
        """Return "acked", "no_leader" or a short failure reason."""
        from agentctl.integrations.registry import adapter_for_agent

        lease = self.store.leases.get(workspace.id)
        if lease is None or not lease.held or self._expired(lease):
            return "no_leader"
        session = None
        if item.event.session_id:
            session = self.store.sessions.find_by_id(item.event.session_id)
        adapter = adapter_for_agent(workspace, None)
        if adapter is None:
            return "no_leader"
        try:
            delivered = adapter.notify_leader(item.event.to_dict(), lease=lease, session=session)
        except Exception as exc:  # noqa: BLE001 - a failing adapter must not kill the daemon
            return f"notify failed: {exc}"
        return "acked" if delivered else "no_leader"

    def _expired(self, lease) -> bool:
        from agentctl.clock import elapsed_since

        age = elapsed_since(lease.expires_at)
        return age is None or age >= 0


class Daemon:
    def __init__(
        self,
        store,
        runtime: RuntimeAdapter,
        workspace: Workspace,
        config: DaemonConfig,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.workspace = workspace
        self.config = config
        self.bus = EventBus(store)
        self.leaders = LeaderService(store, lease_seconds=config.lease_seconds)
        self.lifecycle = LifecycleManager(store, runtime)
        self.messages = MessageService(store, runtime, self.lifecycle, bus=self.bus)
        self.ingestor = Ingestor(store, self.bus)
        self.notifier = Notifier(store, self.bus)
        self._stop = threading.Event()
        self._stream: subprocess.Popen[str] | None = None
        self._lease = None
        self._leader_expired = False
        self._log_lines: list[str] = []

    # ------------------------------------------------------------- lifecycle

    def log(self, message: str) -> None:
        line = f"{now()} {message}"
        self._log_lines.append(line)
        if len(self._log_lines) > 500:
            del self._log_lines[:100]
        try:
            with open(log_path(), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def attach_leader(self) -> None:
        """Take the lease, or take over an expired one.

        A daemon restart is not a new Leader generation when the previous epoch
        is still fresh, so it reuses the stored instance id where possible.
        """
        instance = self.config.instance_id
        try:
            lease, created = self.leaders.attach(self.workspace, instance_id=instance)
        except Exception:
            lease = self.leaders.takeover(self.workspace, instance_id=instance)
            created = True
        self._lease = lease
        self.bus.emit(
            self.workspace,
            "leader.attached",
            source="agentctld",
            leader_epoch=lease.leader_epoch,
            payload={"instance": lease.leader_instance_id, "created": created},
            dedupe_key=f"attached:{lease.leader_epoch}",
        )
        self.log(f"leader attached epoch={lease.leader_epoch} instance={lease.leader_instance_id}")

    def write_state(self) -> None:
        payload = {
            "pid": os.getpid(),
            "workspace_id": self.workspace.id,
            "instance_id": self.config.instance_id,
            "epoch": self._lease.leader_epoch if self._lease else None,
            "socket": str(socket_path()),
            "started_at": now(),
            "log": str(log_path()),
        }
        path = state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)

    def release(self) -> None:
        try:
            if self._lease is not None:
                self.leaders.release(
                    self.workspace, self._lease.lease_token, instance_id=self.config.instance_id
                )
                self.bus.emit(
                    self.workspace,
                    "leader.detached",
                    source="agentctld",
                    leader_epoch=self._lease.leader_epoch,
                    dedupe_key=f"detached:{self._lease.leader_epoch}",
                )
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        try:
            if state_path().exists():
                state = json.loads(state_path().read_text())
                if state.get("pid") == os.getpid():
                    state_path().unlink()
        except (OSError, ValueError):
            pass
        socket_path().unlink(missing_ok=True)

    # ----------------------------------------------------------------- ticks

    def heartbeat(self) -> None:
        if self._lease is None:
            return
        try:
            self._lease = self.leaders.renew(
                self.workspace, self._lease.lease_token, instance_id=self.config.instance_id
            )
            self._leader_expired = False
        except Exception:
            self.log("lease lost; another Leader took over")
            self._leader_expired = True
            self.bus.emit(
                self.workspace,
                "leader.expired",
                source="agentctld",
                dedupe_key=f"expired:{self._lease.leader_epoch}",
            )

    def pump_messages(self) -> None:
        """Dispatch whatever can be dispatched, serially per worker."""
        if self._leader_expired:
            return
        self.messages.recover_stuck(self.workspace)
        self.messages.expire_unacknowledged(self.workspace)
        for session in self.store.sessions.list_for_workspace(self.workspace.id):
            if self.store.messages.active_for_session(session.id) is not None:
                continue
            queued = self.store.messages.next_queued_for_session(session.id)
            if queued is None:
                continue
            try:
                self.messages.dispatch(self.workspace, queued.id)
                self.log(f"dispatched {queued.id} to {session.name}")
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the pump
                self.log(f"dispatch of {queued.id} refused: {exc}")
                self._defer(f"message:{queued.id}", str(exc))

    def pump_outbox(self) -> None:
        if self._leader_expired:
            return
        for item in self.store.outbox.due(self.workspace.id, at=now_precise()):
            try:
                self._run_outbox(item)
                self.store.outbox.finish(item.id, state="done")
            except Exception as exc:  # noqa: BLE001
                self.store.outbox.defer(
                    item.id,
                    not_before=plus(now_precise(), min(60.0, 2.0 ** item.attempt_count)),
                    last_error=str(exc),
                )

    def _run_outbox(self, item: OutboxItem) -> None:
        if item.kind == "recover_session":
            session = self.store.sessions.find_by_id(item.subject_id)
            if session is not None and session.alive:
                self.lifecycle.refresh(session)
        elif item.kind == "refresh_session":
            session = self.store.sessions.find_by_id(item.subject_id)
            if session is not None and session.alive:
                self.lifecycle.refresh(session)
        else:
            self.log(f"unknown outbox kind {item.kind!r}")

    def _defer(self, key: str, reason: str) -> None:
        self.store.outbox.enqueue(
            OutboxItem(
                id=key,
                workspace_id=self.workspace.id,
                kind="message_retry",
                subject_id=key,
                state="pending",
                attempt_count=0,
                not_before=plus(now_precise(), 5.0),
                created_at=now(),
                last_error=reason,
            )
        )

    def pump_inbox(self) -> None:
        """Offer pending events to the Leader, honestly reporting failure."""
        if self._leader_expired:
            return
        for item in self.bus.pending_for_leader(self.workspace, limit=10):
            outcome = self.notifier.deliver(self.workspace, item)
            if outcome == "acked":
                self.store.inbox.ack(item.id, stamp=now_precise())
                self.log(f"notified leader about {item.event.event} ({item.event.id})")
            else:
                # Left pending on purpose: a Leader that is not running must be
                # able to pick it up later, and the backoff stops a busy loop.
                self.store.outbox.enqueue(
                    OutboxItem(
                        id=f"notify:{item.id}",
                        workspace_id=self.workspace.id,
                        kind="notify",
                        subject_id=item.id,
                        state="pending",
                        attempt_count=0,
                        not_before=plus(now_precise(), min(REDELIVER_AFTER_SECONDS, 10.0)),
                        created_at=now(),
                        last_error=outcome,
                    )
                )
                break

    # ----------------------------------------------------------------- stream

    def subscribe(self) -> None:
        """Follow the Herdr Core event stream, reconnecting when it drops."""
        while not self._stop.is_set():
            try:
                self._follow_once()
            except Exception as exc:  # noqa: BLE001
                self.log(f"event stream ended: {exc}")
            if self._stop.is_set():
                return
            time.sleep(min(10.0, 2.0))

    def _follow_once(self) -> None:
        if not self.runtime.available():
            raise RuntimeError("hc is not available")
        argv = event_command("hc", labels={"agentctl.workspace": self.workspace.id})
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stream = proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                parsed = parse_event_line(line)
                if parsed is None:
                    continue
                kind, body = parsed
                if kind == "snapshot":
                    self._apply_snapshot(body)
                elif kind == "event":
                    self._apply_event(body)
                # heartbeat: nothing to do, it only proves the stream is alive
        finally:
            self._stream = None
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _apply_snapshot(self, body: dict) -> None:
        for terminal in body.get("terminals") or []:
            self.ingestor.observe(self.workspace, TerminalView.parse(terminal))

    def _apply_event(self, params: dict) -> None:
        kind = params.get("type")
        data = params.get("data") or {}
        terminal = params.get("terminal")
        if kind == "status":
            status = data.get("status")
            word = status.get("state") if isinstance(status, dict) else status
            view = TerminalView(
                address=terminal,
                status=word,
                reported=bool(data.get("reported")),
                turn=int(params.get("turn") or 0),
            )
            self._observe_with_stored_turn(view)
        elif kind == "turn":
            self._observe_with_stored_turn(
                TerminalView(address=terminal, status="done", reported=True, turn=int(data.get("turn") or 0))
            )
        elif kind == "finished":
            self.ingestor.observe(
                self.workspace,
                TerminalView(
                    address=terminal,
                    state="finished",
                    reason=data.get("reason"),
                    exit_code=data.get("exit_code"),
                ),
            )
        elif kind == "created":
            self.ingestor.observe(
                self.workspace,
                TerminalView.parse({**data, "address": data.get("address") or terminal}),
            )

    def _observe_with_stored_turn(self, view: TerminalView) -> None:
        """Herdr Core's event payloads may omit `turn`; fall back to the row."""
        if not view.turn:
            session = self.store.sessions.find_by_id(view.labels.get("agentctl.session") or "")
            if session is None:
                for candidate in self.store.sessions.list_for_workspace(self.workspace.id):
                    if candidate.hc_terminal_id == view.address:
                        session = candidate
                        break
            if session is not None:
                view.turn = int(getattr(session, "runtime_turn", 0) or 0)
        self.ingestor.observe(self.workspace, view)

    # ------------------------------------------------------------------- run

    def run_forever(self) -> None:
        threads = [
            threading.Thread(target=self.subscribe, name="events", daemon=True),
        ]
        for thread in threads:
            thread.start()
        last_heartbeat = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            if started - last_heartbeat >= max(5.0, self.config.lease_seconds / 3):
                self.heartbeat()
                last_heartbeat = started
            for step in (self.pump_messages, self.pump_outbox, self.pump_inbox):
                try:
                    step()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{step.__name__} failed: {exc}")
            self._stop.wait(self.config.tick)

    def run_once(self) -> dict[str, object]:
        """One full pass, for tests and for `daemon run --once`."""
        self.pump_messages()
        self.pump_outbox()
        self.pump_inbox()
        return {
            "workspace_id": self.workspace.id,
            "pending_inbox": self.store.inbox.count_pending(self.workspace.id),
        }

    def install_signal_handlers(self) -> None:
        def stop(_signum, _frame):
            self._stop.set()
            if self._stream is not None:
                self._stream.terminate()

        for signal_name in ("SIGTERM", "SIGINT"):
            handler = getattr(signal, signal_name, None)
            if handler is not None:
                try:
                    signal.signal(handler, stop)
                except ValueError:  # pragma: no cover - not the main thread
                    pass


def read_state() -> dict[str, object] | None:
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_state() -> dict[str, object] | None:
    state = read_state()
    if state is None:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return None
    return state
