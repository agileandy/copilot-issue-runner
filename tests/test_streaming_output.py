import json

from issue_runner.copilot import _parse_event_line
from issue_runner.events import EventBus, RunEvent
from issue_runner.tui import RunnerApp


def visible_rows(app):
    output = app.query_one("#agent")
    return [output.render_line(row).text for row in range(output.content_size.height)]


def stream(app, *chunks):
    for chunk in chunks:
        app.apply_event(RunEvent("agent_output", {"chunk": chunk}))


async def test_stream_fragments_form_a_single_readable_line():
    app = RunnerApp(EventBus())
    async with app.run_test(size=(100, 30)) as pilot:
        stream(app, "A streamed", " sentence", ".")
        await pilot.pause()
        assert any("A streamed sentence." in row for row in visible_rows(app))


async def test_long_output_wraps_without_losing_characters_in_a_narrow_pane():
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 5 + "END-MARKER"
    app = RunnerApp(EventBus())
    async with app.run_test(size=(70, 32)) as pilot:
        stream(app, text)
        await pilot.pause()
        rendered = "".join(row.strip() for row in visible_rows(app))
        assert text in rendered


async def test_existing_output_reflows_when_the_terminal_narrows():
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 5 + "END-MARKER"
    app = RunnerApp(EventBus())
    async with app.run_test(size=(160, 32)) as pilot:
        stream(app, text)
        await pilot.pause()
        assert text in "".join(row.strip() for row in visible_rows(app))
        await pilot.resize_terminal(70, 32)
        await pilot.pause()
        assert text in "".join(row.strip() for row in visible_rows(app))


async def test_output_remains_read_only_and_keeps_recent_history():
    app = RunnerApp(EventBus())
    async with app.run_test(size=(100, 32)) as pilot:
        stream(app, "\n".join(f"line-{number}" for number in range(2005)))
        await pilot.pause()
        output = app.query_one("#agent")
        assert output.document.line_count == 2000
        assert output.text.startswith("line-5\n")
        assert output.text.endswith("line-2004")
        assert any("line-2004" in row for row in visible_rows(app))
        previous = output.text
        await pilot.press("x")
        assert output.text == previous


async def test_full_blocked_reason_is_available_in_the_output():
    reason = "failure details " * 30 + "END-OF-FAILURE"
    app = RunnerApp(EventBus())
    async with app.run_test(size=(100, 32)) as pilot:
        app.apply_event(RunEvent("ticket_blocked", {"reason": reason}))
        await pilot.pause()
        assert reason in app.query_one("#agent").text


def test_tool_arguments_are_not_silently_cut_at_120_characters():
    command = "echo " + "long-command-argument-" * 30 + "END-OF-COMMAND"
    event = json.dumps(
        {
            "type": "tool.execution_start",
            "data": {"toolName": "bash", "arguments": {"command": command}},
        }
    )
    chunk, _, _, _ = _parse_event_line(event)
    assert command in chunk
