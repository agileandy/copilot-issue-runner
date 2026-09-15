from issue_runner.events import EventBus, RunEvent
from issue_runner.tui import (
    RunnerApp,
    format_board,
    format_pipeline,
    format_stats,
    format_summary,
    help_text,
)

# -- pure formatting -------------------------------------------------------


def test_pipeline_marks_current_phase_and_done_phases():
    out = format_pipeline({"phase": "build", "branch": "issue-9-x"})
    assert "plan ✔" in out
    assert "branch ✔" in out
    assert "[bold reverse] build [/]" in out
    assert "finished ○" in out
    assert "issue-9-x" in out


def test_board_shows_status_glyphs_and_blocked_reason():
    tickets = [
        {"id": 1, "title": "one", "status": "done", "rounds": 0, "blocked_reason": None},
        {"id": 2, "title": "two", "status": "in_progress", "rounds": 2, "blocked_reason": None},
        {
            "id": 3,
            "title": "three",
            "status": "blocked",
            "rounds": 3,
            "blocked_reason": "verifier refused",
        },
    ]
    out = format_board(tickets)
    assert "✔ #1 one" in out
    assert "● #2 two" in out and "rounds 2" in out
    assert "⚠ #3 three" in out and "verifier refused" in out


def test_board_empty_state():
    assert "planner is thinking" in format_board([])


def test_stats_line_formats_tokens_and_current_call():
    out = format_stats(
        {
            "calls": 7,
            "input_tokens": 41000,
            "output_tokens": 2000,
            "run_elapsed": 125,
            "current_role": "builder.coder",
            "call_elapsed": 61,
        }
    )
    assert "calls [bold]7[/]" in out
    assert "41,000" in out
    assert "2m05s" in out
    assert "builder.coder" in out and "61s" in out


def test_stats_distinguishes_zero_unknown_and_partial_costs():
    assert "0.00 AIU" in format_stats({"calls": 1, "nano_aiu": 0})
    assert "unknown" in format_stats({"calls": 1})
    assert "at least 7.00 AIU" in format_stats(
        {"calls": 1, "nano_aiu": 7_000_000_000, "unknown_cost_calls": 1}
    )


# -- app smoke (headless pilot) -------------------------------------------


async def test_app_holds_a_finished_state_for_review_instead_of_exiting():
    """Regression: the TUI used to self-close 1.5s after the run finished."""
    bus = EventBus()
    app = RunnerApp(bus)
    async with app.run_test() as pilot:
        app.apply_event(
            RunEvent(
                "tickets_updated",
                {
                    "tickets": [
                        {
                            "id": 1,
                            "title": "wire flag",
                            "status": "in_progress",
                            "rounds": 0,
                            "blocked_reason": None,
                        }
                    ],
                },
            )
        )
        app.apply_event(
            RunEvent("agent_call_started", {"role": "builder.tester", "session": "tester-t1"})
        )
        app.apply_event(
            RunEvent(
                "agent_output", {"role": "builder.tester", "chunk": "writing the failing test"}
            )
        )
        await pilot.pause()
        board = app.query_one("#board").render()
        assert "wire flag" in str(board)
        assert app.stats["calls"] == 1

        app.apply_event(
            RunEvent(
                "run_finished",
                {
                    "done": 1,
                    "blocked": 0,
                    "branch": "b",
                    "pr_url": "https://x/pull/2",
                    "usage": "usage — calls: 4",
                },
            )
        )
        await pilot.pause(delay=2.0)

        assert app.finished is True
        assert app.is_running, "the app must stay open for the user to review"
        summary = str(app.query_one("#summary").render())
        assert "RUN FINISHED" in summary
        assert "https://x/pull/2" in summary
        assert "usage — calls: 4" in summary

        await pilot.press("q")
    assert app.return_value == "finished"


async def test_q_suspends_and_reattaches_without_destroying_the_app(monkeypatch):
    from contextlib import contextmanager

    from issue_runner import tui

    bus = EventBus()
    app = RunnerApp(bus)
    suspended = []

    @contextmanager
    def suspend():
        suspended.append(app.detached)
        yield

    monkeypatch.setattr(app, "suspend", suspend)
    monkeypatch.setattr(tui, "_terminal_key", lambda: "r")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.finished is False
        await pilot.press("q")
        await pilot.pause()
        assert suspended == [True]
        assert app.is_running and not app.detached and not app.finished


def test_display_drains_a_bounded_batch_so_terminal_controls_can_run(monkeypatch):
    app = RunnerApp(EventBus())
    count = 0

    def next_event():
        nonlocal count
        count += 1
        if count > 1000:
            raise AssertionError("streaming events starved the terminal control loop")
        return RunEvent("agent_output", {"chunk": "streamed output"})

    monkeypatch.setattr(app._queue, "get_nowait", next_event)
    monkeypatch.setattr(app, "apply_event", lambda event: None)
    app._drain()
    assert 0 < count <= 1000


async def test_summary_panel_is_hidden_until_the_run_finishes():
    bus = EventBus()
    app = RunnerApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#summary").display is False
        app.apply_event(RunEvent("run_finished", {"done": 0, "blocked": 2, "branch": "b"}))
        await pilot.pause()
        assert app.query_one("#summary").display is True


def test_summary_reports_blocked_tickets():
    out = format_summary({"done": 1, "blocked": 2, "branch": "b"})
    assert "2 blocked" in out
    assert "press q to close" in out


def test_summary_reports_a_budget_stop():
    out = format_summary({"done": 1, "blocked": 1, "budget_exhausted": True})
    assert "credit budget exhausted" in out


def test_summary_reports_a_clean_run():
    assert "all tickets done" in format_summary({"done": 3, "blocked": 0, "branch": "b"})


def test_plan_only_summary_does_not_claim_tickets_were_executed():
    result = format_summary({"done": 0, "blocked": 0, "plan_only": True})
    assert "plan ready" in result
    assert "all tickets done" not in result


async def test_bus_events_reach_app_through_queue():
    bus = EventBus()
    app = RunnerApp(bus)
    async with app.run_test() as pilot:
        bus.emit("phase", name="build")  # emitted from "another thread" via queue
        await pilot.pause(delay=0.3)
        assert app.state["phase"] == "build"


def test_summary_reports_an_aborted_run():
    out = format_summary({"done": 0, "blocked": 0, "error": "planner failed: no JSON"})
    assert "run aborted" in out
    assert "planner failed" in out


async def test_pipeline_crash_still_reaches_the_finished_state(monkeypatch):
    """A crash must not leave the TUI on a live-looking display forever."""
    from issue_runner import orchestrator, tui

    def boom(cfg, client, issue, plan_only=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(orchestrator, "run_issue", boom)

    seen = []

    class StubApp:
        """Stands in for the Textual app: runs the pipeline, records the bus."""

        def __init__(self, bus, pipeline_thread=None, config=None):
            bus.subscribe(seen.append)
            self._thread = pipeline_thread
            self.finished = True

        def run(self):
            self._thread.start()
            self._thread.join()
            return "finished"

    monkeypatch.setattr(tui, "RunnerApp", StubApp)

    class Cfg:
        events = None

    report, error, detached = tui.run_visual(Cfg(), object(), {"number": 1, "title": "t"})

    assert report is None
    assert isinstance(error, RuntimeError)
    assert detached is False
    finished = [e for e in seen if e.kind == "run_finished"]
    assert finished, "a crash must still emit run_finished so the TUI can settle"
    assert finished[0].payload["error"] == "kaboom"
    assert "run aborted" in format_summary(finished[0].payload)


# -- help screen -----------------------------------------------------------


def test_help_text_explains_the_workflow_controls_and_the_determinism_balance():
    text = help_text()
    for role in ("planner", "builder.tester", "builder.coder", "verifier"):
        assert role in text
    for control in ("regression", "worktree", "max_rounds", "budget"):
        assert control in text.lower()
    assert "probabilistic" in text.lower() and "deterministic" in text.lower()
    assert "red" in text.lower() and "green" in text.lower()
    for key in ("h", "q", "Ctrl+C"):
        assert key in text


async def test_h_toggles_the_help_overlay_without_stopping_the_run():
    app = RunnerApp(EventBus())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#help").display is False
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#help").display is True
        assert app.is_running and not app.finished
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#help").display is False


async def test_escape_closes_the_help_overlay_and_q_still_detaches(monkeypatch):
    from contextlib import contextmanager

    from issue_runner import tui

    app = RunnerApp(EventBus())

    @contextmanager
    def suspend():
        yield

    monkeypatch.setattr(app, "suspend", suspend)
    monkeypatch.setattr(tui, "_terminal_key", lambda: "r")
    async with app.run_test() as pilot:
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#help").display is False
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running


async def test_help_panel_scrolls_when_the_text_is_taller_than_the_screen():
    app = RunnerApp(EventBus())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.press("h")
        await pilot.pause()
        panel = app.query_one("#help")
        assert panel.is_container and panel.allow_vertical_scroll
        assert panel.max_scroll_y > 0, "the help text must be taller than the panel"
        assert app.focused is panel
        await pilot.press("pagedown")
        await pilot.pause()
        assert panel.scroll_offset.y > 0
        await pilot.press("end")
        await pilot.pause()
        assert panel.scroll_offset.y == panel.max_scroll_y


async def test_closing_help_returns_focus_so_keys_still_work():
    app = RunnerApp(EventBus())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#help").display is False
        assert app.focused is not app.query_one("#help")
