"""Streamed agent output must survive the transport intact."""

import json

from issue_runner.copilot import _parse_event_line


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
