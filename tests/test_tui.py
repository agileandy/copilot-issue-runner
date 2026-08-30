from issue_runner.events import EventBus, RunEvent
from issue_runner.tui import RunnerApp, format_board, format_pipeline, format_stats

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


# -- app smoke (headless pilot) -------------------------------------------


async def test_app_applies_events_and_exits_on_run_finished():
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

        app.apply_event(RunEvent("run_finished", {"done": 1, "blocked": 0, "branch": "b"}))
        await pilot.pause(delay=2.0)
    assert app.finished is True
    assert app.return_value == "finished"


async def test_bus_events_reach_app_through_queue():
    bus = EventBus()
    app = RunnerApp(bus)
    async with app.run_test() as pilot:
        bus.emit("phase", name="build")  # emitted from "another thread" via queue
        await pilot.pause(delay=0.3)
        assert app.state["phase"] == "build"
