"""Tiled in-process TUI for --visual (issue #19).

Four tiles on one contained alt-screen: pipeline banner, ticket board, live
agent output, run stats. The pipeline runs in a plain (non-daemon) thread and
publishes on the EventBus; the app drains a thread-safe queue on a timer, so
Textual never blocks the run and `q` merely detaches the display — the run
continues headless and the CLI prints the summary after the app exits.
"""

import queue
import threading
import time
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, RichLog, Static

from .events import EventBus, RunEvent
from .usage import format_aiu

STATUS_GLYPHS = {
    "pending": "○",
    "in_progress": "●",
    "done": "✔",
    "blocked": "⚠",
}

PHASES = ("plan", "branch", "build", "finished")


def format_pipeline(state: dict) -> str:
    current = state.get("phase", "plan")
    parts = []
    reached = True
    for phase in PHASES:
        if phase == current:
            parts.append(f"[bold reverse] {phase} [/]")
            reached = False
        elif reached:
            parts.append(f"[green]{phase} ✔[/]")
        else:
            parts.append(f"[dim]{phase} ○[/]")
    line = "  →  ".join(parts)
    if state.get("branch"):
        line += f"    [dim]branch:[/] {state['branch']}"
    return line


def format_board(tickets: list[dict]) -> str:
    if not tickets:
        return "[dim]no tickets yet — planner is thinking[/]"
    lines = []
    for t in tickets:
        glyph = STATUS_GLYPHS.get(t["status"], "?")
        style = {
            "done": "green",
            "in_progress": "yellow",
            "blocked": "red",
            "pending": "dim",
        }.get(t["status"], "")
        line = f"[{style}]{glyph} #{t['id']} {t['title']}[/]"
        if t.get("rounds"):
            line += f" [dim](rounds {t['rounds']})[/]"
        if t["status"] == "blocked" and t.get("blocked_reason"):
            line += f"\n    [red dim]{t['blocked_reason'][:110]}[/]"
        lines.append(line)
    return "\n".join(lines)


def format_stats(stats: dict) -> str:
    tokens_in = stats.get("input_tokens", 0)
    tokens_out = stats.get("output_tokens", 0)
    line = (
        f"calls [bold]{stats.get('calls', 0)}[/]"
        f"  ·  tokens in [bold]{tokens_in:,}[/] / out [bold]{tokens_out:,}[/]"
    )
    if stats.get("nano_aiu"):
        line += f"  ·  credits [bold]{format_aiu(stats['nano_aiu'])}[/]"
    line += (
        f"  ·  run [bold]{int(stats.get('run_elapsed', 0)) // 60}m"
        f"{int(stats.get('run_elapsed', 0)) % 60:02d}s[/]"
    )
    if stats.get("current_role"):
        line += (
            f"  ·  [yellow]{stats['current_role']}[/] working {int(stats.get('call_elapsed', 0))}s"
        )
    return line


def format_summary(summary: dict) -> str:
    """The end-of-run panel. Shown only once the pipeline has finished."""
    done = summary.get("done", 0)
    blocked = summary.get("blocked", 0)
    if summary.get("error"):
        outcome = "[red]run aborted[/]"
    elif summary.get("budget_exhausted"):
        outcome = "[red]stopped: credit budget exhausted[/]"
    elif blocked:
        outcome = f"[red]{blocked} blocked[/]"
    else:
        outcome = "[green]all tickets done[/]"
    lines = [
        f"[bold reverse] RUN FINISHED [/]  {outcome}",
        f"tickets done [bold]{done}[/]  ·  blocked [bold]{blocked}[/]",
    ]
    if summary.get("error"):
        lines.append(f"[red]{summary['error'][:300]}[/]")
    if summary.get("branch"):
        lines.append(f"branch [bold]{summary['branch']}[/]")
    if summary.get("pr_url"):
        lines.append(f"pull request {summary['pr_url']}")
    if summary.get("usage"):
        lines.append(f"[dim]{summary['usage']}[/]")
    lines.append("[dim]press q to close — the summary is also printed on exit[/]")
    return "\n".join(lines)


class RunnerApp(App):
    TITLE = "issue-runner"
    BINDINGS: ClassVar = [("q", "close", "detach / close")]
    CSS = """
    #pipeline { height: 3; padding: 1 2 0 2; }
    #middle { height: 1fr; }
    #board { width: 42%; border: round $primary; padding: 0 1; }
    #agent { width: 58%; border: round $secondary; }
    #stats { height: 3; border: round $accent; padding: 0 1; content-align: left middle; }
    #summary { height: auto; border: round $success; padding: 0 1; }
    """

    def __init__(self, bus: EventBus, pipeline_thread: threading.Thread | None = None):
        super().__init__()
        self._queue: queue.Queue = queue.Queue()
        bus.subscribe(self._queue.put)
        self._thread = pipeline_thread
        self._started = time.monotonic()
        self._call_started: float | None = None
        self.state: dict = {"phase": "plan", "branch": "", "tickets": []}
        self.stats: dict = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.summary: dict = {}
        self.finished = False

    def compose(self) -> ComposeResult:
        yield Static(id="pipeline")
        with Horizontal(id="middle"):
            yield Static(id="board")
            yield RichLog(id="agent", wrap=True, markup=False, max_lines=2000)
        yield Static(id="stats")
        summary = Static(id="summary")
        summary.display = False  # revealed only when the run finishes
        yield summary
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self._drain)
        self.set_interval(1.0, self._refresh_stats)
        self._render_all()
        if self._thread is not None:
            self._thread.start()

    def action_close(self) -> None:
        """Before the run ends `q` detaches; afterwards it closes the review."""
        self.exit("finished" if self.finished else "detached")

    # -- event application -------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self.apply_event(event)

    def apply_event(self, event: RunEvent) -> None:
        kind, p = event.kind, event.payload
        if kind == "phase":
            self.state["phase"] = p["name"]
        elif kind == "run_started":
            self.state["issue"] = p.get("title", "")
        elif kind == "tickets_updated":
            self.state["tickets"] = p["tickets"]
        elif kind == "run_finished":
            # hold the display: the user reviews the board and closes with `q`
            self.state["phase"] = "finished"
            self.state["branch"] = p.get("branch", self.state.get("branch", ""))
            self.finished = True
            self.summary = {
                "done": p.get("done", 0),
                "blocked": p.get("blocked", 0),
                "branch": self.state["branch"],
                "pr_url": p.get("pr_url", ""),
                "usage": p.get("usage", ""),
                "budget_exhausted": p.get("budget_exhausted", False),
                "error": p.get("error", ""),
            }
            self.query_one("#summary", Static).display = True
        elif kind == "agent_call_started":
            self.stats["calls"] += 1
            self.stats["current_role"] = p.get("role")
            self._call_started = time.monotonic()
            log = self.query_one("#agent", RichLog)
            log.write(f"\n━━ {p.get('role')} ({p.get('session') or 'session'}) ━━")
        elif kind == "agent_output":
            self.query_one("#agent", RichLog).write(p.get("chunk", ""), scroll_end=True)
        elif kind == "agent_call_finished":
            self.stats["current_role"] = None
            self._call_started = None
            usage = p.get("usage") or {}
            self.stats["input_tokens"] += usage.get("input_tokens", 0)
            self.stats["output_tokens"] += usage.get("output_tokens", 0)
            self.stats["nano_aiu"] = self.stats.get("nano_aiu", 0) + usage.get("nano_aiu", 0)
            self.query_one("#agent", RichLog).write(f"── done in {p.get('elapsed', '?')}s ──")
        elif kind == "ticket_blocked":
            self.query_one("#agent", RichLog).write(f"⚠ BLOCKED: {p.get('reason', '')[:300]}")
        if kind in ("phase", "run_started", "tickets_updated", "run_finished", "ticket_blocked"):
            self._render_all()

    # -- rendering ---------------------------------------------------------

    def _render_all(self) -> None:
        self.query_one("#pipeline", Static).update(format_pipeline(self.state))
        self.query_one("#board", Static).update(format_board(self.state["tickets"]))
        if self.finished:
            self.query_one("#summary", Static).update(format_summary(self.summary))
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        self.stats["run_elapsed"] = time.monotonic() - self._started
        if self._call_started is not None:
            self.stats["call_elapsed"] = time.monotonic() - self._call_started
        else:
            self.stats["call_elapsed"] = 0
        self.query_one("#stats", Static).update(format_stats(self.stats))


def run_visual(cfg, client, issue: dict, plan_only: bool = False):
    """Run the pipeline under the TUI. Returns (report | None, error | None, detached)."""
    from .orchestrator import run_issue  # local import: avoid cycles

    bus = EventBus()
    cfg.events = bus
    result: dict = {}

    def target():
        try:
            result["report"] = run_issue(cfg, client, issue, plan_only=plan_only)
        except BaseException as e:  # noqa: BLE001 — surfaced after teardown
            result["error"] = e
            # without this the TUI would sit on a live-looking display forever
            bus.emit("run_finished", done=0, blocked=0, branch="", error=str(e))

    thread = threading.Thread(target=target, name="issue-runner-pipeline")
    app = RunnerApp(bus, pipeline_thread=thread)
    outcome = app.run()

    detached = outcome == "detached" and thread.is_alive()
    if detached:
        print("display detached — run continues headless; progress below:")
        bus.subscribe(lambda e: _print_event(e))
    thread.join()
    return result.get("report"), result.get("error"), detached


def _print_event(event: RunEvent) -> None:
    if event.kind in (
        "phase",
        "ticket_started",
        "verdict",
        "ticket_done",
        "ticket_blocked",
        "run_finished",
    ):
        print(f"[{event.kind}] {event.payload}")
