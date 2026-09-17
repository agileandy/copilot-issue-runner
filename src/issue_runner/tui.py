"""Tiled in-process TUI for --visual (issue #19).

Four tiles on one contained alt-screen: pipeline banner, ticket board, live
agent output, run stats. The pipeline runs in a plain (non-daemon) thread and
publishes on the EventBus; the app drains a thread-safe queue on a timer, so
Textual never blocks the run. `q` suspends the same display so it can reattach
without losing its board or output. Ctrl+C requests a checkpointed stop.
"""

import os
import queue
import select
import sys
import threading
import time
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Static, TextArea

from .config import RunnerConfig
from .control import announce_stop, request_stop
from .events import EventBus, RunEvent
from .usage import cost_is_complete, format_aiu

STATUS_GLYPHS = {
    "pending": "○",
    "in_progress": "●",
    "done": "✔",
    "blocked": "⚠",
}

PHASES = ("plan", "branch", "build", "finished")

HELP = """\
[bold]issue-runner[/] — one GitHub issue becomes a chain of small, proven commits.

[bold]Keys[/]
  [bold]h[/] help on/off      [bold]escape[/] close this panel
  [bold]↑ ↓ PageUp PageDown Home End[/] scroll this panel
  [bold]q[/] detach the display — the run keeps going; type r + Enter to reattach
  [bold]Ctrl+C[/] stop cleanly at the next model call, keeping state and the worktree

[bold]Roles and what each prompt is given[/]
  [bold]planner[/]        sees the issue title and body only. Splits it into small tickets, each
                 with ONE test assertion and its dependencies. It writes no code.
  [bold]builder.tester[/] sees one ticket. Writes a single failing test for that assertion, and
                 nothing else. It may not touch source files.
  [bold]builder.coder[/]  sees the ticket and the accepted test. Writes the least code that makes
                 the test pass. The test file is frozen: it cannot edit the test to pass.
  [bold]verifier[/]       sees the ticket, the test and the diff, read-only. Answers pass,
                 refine_test or rework_code, with the reason fed back to the right role.

[bold]Where the agents' conversation lives[/]
  Every handoff is posted as a comment on that ticket's own sub-issue: the planner's brief,
  the accepted test, the implementation ready for review, the verdict — and every hand-back,
  such as a rejected stub or a failed regression. A ticket that goes right first time is
  documented just as fully as one that takes four rounds. When work IS sent back, the next
  prompt does not repeat the reason; it sends the agent to the issue to read the thread.
  Each sub-issue is labelled "in progress" as its ticket starts, and that label is cleared
  when the ticket is closed as completed — the last thing that happens before the next
  ticket begins. Status marking is best-effort: a tracker that refuses it never stops work.
  With no tracker (--no-github-tickets, --issue-file) the feedback is inlined as before.

[bold]Checks and controls[/]
  red first        a new test must fail before any code is written; a test that passes on
                   arrival is handed to the verifier to prove it is not a tautology
  frozen test      the accepted test is hashed; if it changes outside the tester phase the
                   ticket stops
  clean worktree   every run works in its own git worktree and branch, never your checkout
  bounded loops    max_rounds caps verifier hand-backs, then the ticket blocks rather than
                   looping forever
  regression gate  the whole suite must pass on the approved workspace before any commit
  guarded commit   only the approved file set is staged, and a commit hook that alters the
                   tree is rejected
  budget           per-call and per-run AI credit caps pause the run instead of overspending
  resumable        state is saved per ticket, so a stopped run resumes where it left off

[bold]Probabilistic generation, deterministic proof[/]
  The model is free to propose: how to split the work, how to phrase a test, how to implement.
  None of that is trusted on its word. Every proposal has to survive something that cannot be
  argued with — a test that must go red then green, a full suite that must stay green, a diff
  that must match the approved file set, a git tree that must match what was verified.
  The AI supplies the judgement, the shell supplies the verdict. When the two disagree, the
  shell wins and the ticket blocks with the reason on the board.
"""


def help_text() -> str:
    return HELP


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
    if stats.get("nano_aiu") is not None:
        qualifier = "at least " if stats.get("unknown_cost_calls") else ""
        line += f"  ·  credits [bold]{qualifier}{format_aiu(stats['nano_aiu'])}[/]"
    elif stats.get("calls"):
        line += "  ·  credits [bold]unknown[/]"
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
    elif summary.get("stopped"):
        outcome = "stopped by user; work saved"
    elif summary.get("budget_exhausted"):
        outcome = "[red]stopped: credit budget exhausted[/]"
    elif summary.get("plan_only"):
        outcome = "plan ready (no tickets executed)"
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
    if summary.get("worktree"):
        lines.append(f"worktree {summary['worktree']}")
    if summary.get("pr_url"):
        lines.append(f"pull request {summary['pr_url']}")
    if summary.get("usage"):
        lines.append(f"[dim]{summary['usage']}[/]")
    if summary.get("budget"):
        lines.append(f"[dim]{summary['budget']}[/]")
    lines.append("[dim]press q to close — the summary is also printed on exit[/]")
    return "\n".join(lines)


class RunnerApp(App):
    TITLE = "issue-runner"
    BINDINGS: ClassVar = [
        Binding("h", "toggle_help", "help", priority=True),
        Binding("escape", "close_help", "close help", show=False),
        Binding("q", "close", "detach / close", priority=True),
        Binding("ctrl+c", "stop_run", "stop", priority=True),
        Binding("ctrl+q", "stop_run", "stop", priority=True, show=False),
    ]
    CSS = """
    #pipeline { height: 3; padding: 1 2 0 2; }
    #middle { height: 1fr; }
    #board { width: 42%; border: round $primary; padding: 0 1; }
    #agent { width: 58%; border: round $secondary; }
    #stats { height: 3; border: round $accent; padding: 0 1; content-align: left middle; }
    #summary { height: auto; border: round $success; padding: 0 1; }
    #help {
        scrollbar-size-vertical: 1;
        layer: overlay;
        width: 100%;
        height: 100%;
        background: $surface;
        border: round $warning;
        padding: 0 1;
        overflow-y: auto;
    }
    Screen { layers: base overlay; }
    """

    def __init__(
        self,
        bus: EventBus,
        pipeline_thread: threading.Thread | None = None,
        config: RunnerConfig | None = None,
    ):
        super().__init__()
        self._queue: queue.Queue = queue.Queue()
        bus.subscribe(self._queue.put)
        self._thread = pipeline_thread
        self._runner_config = config
        self._started = time.monotonic()
        self._call_started: float | None = None
        self.state: dict = {"phase": "plan", "branch": "", "tickets": []}
        self.stats: dict = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.summary: dict = {}
        self.finished = False
        self.detached = False
        self.stop_message = ""

    def compose(self) -> ComposeResult:
        yield Static(id="pipeline")
        with Horizontal(id="middle"):
            yield Static(id="board")
            yield TextArea(
                id="agent",
                read_only=True,
                soft_wrap=True,
                show_line_numbers=False,
                show_cursor=False,
                highlight_cursor_line=False,
            )
        yield Static(id="stats")
        help_panel = VerticalScroll(Static(help_text(), id="help-body"), id="help")
        help_panel.display = False
        help_panel.can_focus = True
        yield help_panel
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

    def action_toggle_help(self) -> None:
        panel = self.query_one("#help")
        if panel.display:
            self.action_close_help()
            return
        panel.display = True
        panel.scroll_home(animate=False)
        panel.focus()

    def action_close_help(self) -> None:
        panel = self.query_one("#help")
        panel.display = False
        self.query_one("#agent").focus()

    def action_close(self) -> None:
        """Before the run ends `q` detaches; afterwards it closes the review."""
        if self.finished:
            self.exit("finished")
            return
        self.detached = True
        try:
            with self.suspend():
                print(
                    "\ndisplay detached - type r and Enter to reattach; Ctrl+C to stop.",
                    flush=True,
                )
                while not self.finished:
                    self._drain()
                    key = _terminal_key()
                    if key == "r":
                        break
                    if key in ("s", "\x03") or (
                        key == ""
                        and self._runner_config is not None
                        and not self._runner_config.control.requested
                    ):
                        self.action_stop_run()
                    # App.suspend stops the writer: yielding to the UI event loop
                    # here would let renderer timers fill its unserviced queue.
                    time.sleep(0.05)
        finally:
            self.detached = False
        self._render_all()
        if self.finished:
            self.exit("stopped" if self.summary.get("stopped") else "finished")

    def action_stop_run(self) -> None:
        if self.finished:
            self.exit("finished")
        elif self._runner_config is not None:
            request_stop(self._runner_config)

    # -- event application -------------------------------------------------

    def _drain(self) -> None:
        if self._runner_config is not None:
            announce_stop(self._runner_config)
        for _ in range(100):
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self.apply_event(event)
            if self.detached:
                _print_event(event)

    def apply_event(self, event: RunEvent) -> None:
        kind, p = event.kind, event.payload
        if kind == "phase":
            self.state["phase"] = p["name"]
        elif kind == "stop_requested":
            self.stop_message = p["message"]
            self._render_all()
        elif kind == "run_started":
            self.state["issue"] = p.get("title", "")
        elif kind == "tickets_updated":
            self.state["tickets"] = p["tickets"]
        elif kind == "run_finished":
            # hold the display: the user reviews the board and closes with `q`
            self.state["phase"] = "finished"
            self.state["branch"] = p.get("branch", self.state.get("branch", ""))
            self.finished = True
            self.stats["current_role"] = None
            self._call_started = None
            self.summary = {
                "done": p.get("done", 0),
                "blocked": p.get("blocked", 0),
                "branch": self.state["branch"],
                "worktree": p.get("worktree", ""),
                "pr_url": p.get("pr_url", ""),
                "usage": p.get("usage", ""),
                "budget": p.get("budget", ""),
                "budget_exhausted": p.get("budget_exhausted", False),
                "plan_only": p.get("plan_only", False),
                "stopped": p.get("stopped", False),
                "error": p.get("error", ""),
            }
            self.query_one("#summary", Static).display = True
        elif kind == "agent_call_started":
            self.stats["calls"] += 1
            self.stats["current_role"] = p.get("role")
            self._call_started = time.monotonic()
            self._append_output(f"\n━━ {p.get('role')} ({p.get('session') or 'session'}) ━━\n")
        elif kind == "agent_output":
            self._append_output(p.get("chunk", ""))
        elif kind == "agent_call_finished":
            self.stats["current_role"] = None
            self._call_started = None
            usage = p.get("usage") or {}
            self.stats["input_tokens"] += usage.get("input_tokens", 0)
            self.stats["output_tokens"] += usage.get("output_tokens", 0)
            cost = usage.get("nano_aiu")
            if cost is not None:
                self.stats["nano_aiu"] = self.stats.get("nano_aiu", 0) + cost
            if not cost_is_complete(usage):
                self.stats["unknown_cost_calls"] = self.stats.get("unknown_cost_calls", 0) + 1
            self._append_output(f"\n── done in {p.get('elapsed', '?')}s ──\n")
        elif kind == "ticket_blocked":
            self._append_output(f"\n⚠ BLOCKED: {p.get('reason', '')}\n")
        if kind in ("phase", "run_started", "tickets_updated", "run_finished", "ticket_blocked"):
            self._render_all()
        if kind == "run_finished" and p.get("stopped") and not self.detached:
            self.exit("stopped")

    # -- rendering ---------------------------------------------------------

    def _append_output(self, text: str) -> None:
        output = self.query_one("#agent", TextArea)
        output.insert(text, output.document.end, maintain_selection_offset=False)
        excess = output.document.line_count - 2000
        if excess > 0:
            output.delete((0, 0), (excess, 0))
        output.scroll_end(animate=False)

    def _render_all(self) -> None:
        if self.detached:
            return
        self.query_one("#pipeline", Static).update(self.stop_message or format_pipeline(self.state))
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
    app = RunnerApp(bus, pipeline_thread=thread, config=cfg)
    outcome = app.run()

    if not app.finished and thread.is_alive():
        request_stop(cfg)
    thread.join()
    return result.get("report"), result.get("error"), outcome == "detached"


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
