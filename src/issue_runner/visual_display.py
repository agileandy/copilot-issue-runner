"""Bridge between the pipeline and the OpenTUI display in `apps/visual`.

The pipeline runs in a plain thread and publishes on the EventBus, exactly as
before. What changed is the display: instead of an in-process Textual app, a
Bun process renders the tiles and owns the terminal, and this module streams
RunEvents to it over a loopback socket and acts on the controls it sends back.

Nothing travels over stdio, so the display keeps the real terminal. Detaching
closes the viewer; reattaching starts a new one and replays a compacted
snapshot, so the board, the totals and the recent output survive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .control import announce_stop, request_stop
from .events import EventBus, RunEvent

log = logging.getLogger("issue_runner")

CONNECT_TIMEOUT = 20.0
OUTPUT_TAIL_CHARS = 20_000
POLL_INTERVAL = 0.05


class VisualUnavailable(RuntimeError):
    """The display cannot be started; the caller falls back to the plain run."""


def viewer_app_dir() -> Path:
    override = os.environ.get("ISSUE_RUNNER_VISUAL_APP")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "apps" / "visual"


def ensure_viewer(app_dir: Path) -> str:
    """Return the bun executable, installing the viewer's deps on first use."""
    bun = shutil.which("bun")
    if bun is None:
        raise VisualUnavailable("bun is not installed — see https://bun.sh")
    if not (app_dir / "src" / "index.ts").is_file():
        raise VisualUnavailable(f"no visual display at {app_dir}")
    if not (app_dir / "node_modules").is_dir():
        print("installing the visual display (one time)...", flush=True)
        result = subprocess.run(
            [bun, "install", "--silent"], cwd=app_dir, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise VisualUnavailable(f"bun install failed: {result.stderr.strip()[:300]}")
    return bun


class ReplayState:
    """Compacts the stream into the few events a fresh display needs.

    A reattached viewer is empty, so it is handed the latest board, phase and
    totals rather than the whole history. Output is replayed as a bounded tail:
    the journal, not the display, is the transcript of record.
    """

    def __init__(self) -> None:
        self.started: dict | None = None
        self.phase = "plan"
        self.tickets: dict | None = None
        self.stop_message = ""
        self.finished: dict | None = None
        self.stats = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "nano_aiu": None,
            "unknown_cost_calls": 0,
        }
        self._output: deque[str] = deque()
        self._output_chars = 0
        self._run_started = time.monotonic()

    def record(self, event: RunEvent) -> None:
        kind, payload = event.kind, event.payload
        if kind == "run_started":
            self.started = dict(payload)
        elif kind == "phase":
            self.phase = payload.get("name", self.phase)
        elif kind == "tickets_updated":
            self.tickets = dict(payload)
        elif kind == "stop_requested":
            self.stop_message = payload.get("message", "")
        elif kind == "run_finished":
            self.finished = dict(payload)
            self.phase = "finished"
        elif kind == "agent_call_started":
            self.stats["calls"] += 1
        elif kind == "agent_call_finished":
            usage = payload.get("usage") or {}
            self.stats["input_tokens"] += usage.get("input_tokens", 0) or 0
            self.stats["output_tokens"] += usage.get("output_tokens", 0) or 0
            cost = usage.get("nano_aiu")
            if cost is not None:
                self.stats["nano_aiu"] = (self.stats["nano_aiu"] or 0) + cost
            if not _cost_is_complete(usage):
                self.stats["unknown_cost_calls"] += 1
        elif kind == "agent_output":
            self._append_output(payload.get("chunk", ""))
        elif kind == "ticket_blocked":
            self._append_output(f"\n⚠ BLOCKED: {payload.get('reason', '')}\n")

    def _append_output(self, chunk: str) -> None:
        if not chunk:
            return
        self._output.append(chunk)
        self._output_chars += len(chunk)
        while self._output_chars > OUTPUT_TAIL_CHARS and len(self._output) > 1:
            self._output_chars -= len(self._output.popleft())

    def replay(self) -> list[dict]:
        events: list[dict] = []
        if self.started is not None:
            events.append({"kind": "run_started", "payload": self.started})
        events.append({"kind": "phase", "payload": {"name": self.phase}})
        if self.tickets is not None:
            events.append({"kind": "tickets_updated", "payload": self.tickets})
        events.append(
            {
                "kind": "stats_snapshot",
                "payload": {
                    **self.stats,
                    "run_elapsed": time.monotonic() - self._run_started,
                },
            }
        )
        tail = "".join(self._output)
        if tail:
            events.append({"kind": "agent_output", "payload": {"chunk": tail}})
        if self.stop_message:
            events.append({"kind": "stop_requested", "payload": {"message": self.stop_message}})
        if self.finished is not None:
            events.append({"kind": "run_finished", "payload": self.finished})
        return events


def _cost_is_complete(usage: dict | None) -> bool:
    if not usage:
        return False
    if "cost_complete" in usage:
        return bool(usage["cost_complete"])
    return usage.get("nano_aiu") is not None


class ViewerSession:
    """One attached display: a listening socket and the Bun process on it."""

    def __init__(self, app_dir: Path, bun: str, log_path: Path | None = None):
        self.app_dir = app_dir
        self.bun = bun
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._buffer = b""
        self._log_file = None

    def start(self, replay: list[dict]) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(CONNECT_TIMEOUT)
        port = self._server.getsockname()[1]

        stderr = subprocess.DEVNULL
        if self.log_path is not None:
            with contextlib.suppress(OSError):
                self._log_file = self.log_path.open("a")
                stderr = self._log_file
        try:
            self.process = subprocess.Popen(
                [self.bun, "run", str(self.app_dir / "src" / "index.ts")],
                cwd=self.app_dir,
                env={**os.environ, "ISSUE_RUNNER_VISUAL_PORT": str(port)},
                stderr=stderr,  # stdin and stdout stay the terminal the display draws on
            )
            self._conn, _ = self._server.accept()
        except (TimeoutError, OSError) as e:
            self.close()
            raise VisualUnavailable(f"the visual display did not start: {e}") from e
        self._conn.setblocking(False)
        for event in replay:
            self.send_raw(event)

    def send(self, event: RunEvent) -> None:
        self.send_raw({"kind": event.kind, "payload": event.payload})

    def send_raw(self, event: dict) -> None:
        if self._conn is None:
            return
        try:
            self._conn.sendall(json.dumps(event, default=str).encode() + b"\n")
        except OSError:
            # the viewer went away; the attached loop notices on its next poll
            self._conn = None

    def controls(self) -> list[str]:
        """Read whatever control messages the viewer has sent since last time."""
        if self._conn is None:
            return []
        try:
            if not select.select([self._conn], [], [], 0)[0]:
                return []
            chunk = self._conn.recv(4096)
        except OSError:
            self._conn = None
            return []
        if not chunk:
            self._conn = None
            return []
        self._buffer += chunk
        messages = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                messages.append(str(json.loads(line).get("type", "")))
            except (ValueError, AttributeError):
                continue
        return messages

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        for sock in (self._conn, self._server):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        self._conn = self._server = None
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=3)
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.close()
            self._log_file = None


@contextlib.contextmanager
def _logs_to_file(path: Path | None):
    """Keep log records off the terminal the viewer is drawing on."""
    root = logging.getLogger()
    if path is None:
        yield
        return
    try:
        handler = logging.FileHandler(path)
    except OSError:
        yield
        return
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous = root.handlers[:]
    root.handlers = [handler]
    try:
        yield
    finally:
        root.handlers = previous
        handler.close()


def run_visual(cfg, client, issue: dict, plan_only: bool = False):
    """Run the pipeline under the display. Returns (report | None, error | None, detached)."""
    from .orchestrator import run_issue  # local import: avoid cycles

    app_dir = viewer_app_dir()
    bun = ensure_viewer(app_dir)  # raises VisualUnavailable before the run starts
    log_path = _log_path(cfg)

    bus = EventBus()
    cfg.events = bus
    events: queue.Queue = queue.Queue()
    bus.subscribe(events.put)
    replay = ReplayState()
    result: dict = {}

    def target():
        try:
            result["report"] = run_issue(cfg, client, issue, plan_only=plan_only)
        except BaseException as e:  # noqa: BLE001 — surfaced after teardown
            result["error"] = e
            # without this the display would sit on a live-looking board forever
            ledger = getattr(client, "usage", None)
            budget = getattr(client, "budget", None)
            bus.emit(
                "run_finished",
                done=0,
                blocked=0,
                branch="",
                error=str(e),
                usage=ledger.summary_line() if ledger is not None else "",
                budget=budget.describe() if budget is not None else "",
            )

    thread = threading.Thread(target=target, name="issue-runner-pipeline")
    started = False
    detached = False
    try:
        while True:
            session = ViewerSession(app_dir, bun, log_path)
            with _logs_to_file(log_path):
                session.start(replay.replay())
                if not started:
                    thread.start()
                    started = True
                try:
                    outcome = _attached(cfg, session, events, replay)
                finally:
                    session.close()
            if outcome != "detach":
                break
            detached = True
            print("\ndisplay detached - type r and Enter to reattach; Ctrl+C to stop.", flush=True)
            if _detached(cfg, events, replay) == "finished":
                break
            detached = False
    finally:
        if started:
            if thread.is_alive() and replay.finished is None:
                request_stop(cfg)
            thread.join()
    return result.get("report"), result.get("error"), detached


def _attached(cfg, session: ViewerSession, events, replay: ReplayState) -> str:
    """Pump events into the viewer until it detaches, closes or dies."""
    while True:
        announce_stop(cfg)
        for event in _drain(events, replay):
            session.send(event)
        for control in session.controls():
            if control == "stop":
                request_stop(cfg)
            elif control == "detach":
                return "detach"
            elif control == "closed":
                return "closed"
        if not session.alive():
            # a crashed or killed viewer must not leave the run invisible
            return "detach"
        time.sleep(POLL_INTERVAL)


def _detached(cfg, events, replay: ReplayState) -> str:
    """Print the stream plainly until the user reattaches or the run ends."""
    while True:
        announce_stop(cfg)
        for event in _drain(events, replay):
            _print_event(event)
        if replay.finished is not None:
            return "finished"
        key = _terminal_key()
        if key == "r":
            return "reattach"
        if key in ("s", "\x03") or (key == "" and not cfg.control.requested):
            request_stop(cfg)
        time.sleep(POLL_INTERVAL)


def _drain(events, replay: ReplayState, limit: int = 200) -> list[RunEvent]:
    """Take a bounded batch so a streaming agent cannot starve the controls."""
    drained = []
    for _ in range(limit):
        try:
            event = events.get_nowait()
        except queue.Empty:
            break
        replay.record(event)
        drained.append(event)
    return drained


def _log_path(cfg) -> Path | None:
    state_dir = getattr(cfg, "repo_dir", None)
    if state_dir is None:
        return None
    directory = Path(state_dir) / ".issue-runner"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return directory / "visual.log"


def _terminal_key() -> str | None:
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch() if msvcrt.kbhit() else None
    if select.select([sys.stdin], [], [], 0)[0]:
        return os.read(sys.stdin.fileno(), 1).decode(errors="replace")
    return None


def _print_event(event: RunEvent) -> None:
    if event.kind == "stop_requested":
        print(event.payload["message"], flush=True)
        return
    if event.kind in (
        "phase",
        "ticket_started",
        "verdict",
        "ticket_done",
        "ticket_blocked",
        "run_finished",
    ):
        print(f"[{event.kind}] {event.payload}")
