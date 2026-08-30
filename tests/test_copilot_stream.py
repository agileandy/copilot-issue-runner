import io
import json

import pytest

from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient, CopilotError
from issue_runner.events import EventBus


def jl(kind, **data):
    return json.dumps({"type": kind, "data": data}) + "\n"


SCRIPT = (
    jl("session.tools_updated", model="qwen")
    + jl("assistant.reasoning_delta", reasoningId="r1", deltaContent="thinking about it")
    + jl(
        "tool.execution_start", toolCallId="c1", toolName="bash", arguments={"command": "pytest -q"}
    )
    + jl("assistant.message_delta", messageId="m1", deltaContent='{"test_path"')
    + jl("assistant.message_delta", messageId="m1", deltaContent=': "tests/test_x.py"}')
    + jl("assistant.message", messageId="m1", content='{"test_path": "tests/test_x.py"}')
    + jl(
        "model.model_call_success",
        responseChunk={"usage": {"input_tokens": 100, "output_tokens": 20}},
    )
)


class FakeProc:
    def __init__(self, lines, returncode=0, hang=False):
        self.stdout = io.StringIO(lines)
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    def wait(self, timeout=None):
        if self._hang and not self.killed:
            raise TimeoutError  # stand-in; client converts via subprocess.TimeoutExpired
        return self.returncode

    def poll(self):
        return None if (self._hang and not self.killed) else self.returncode

    def kill(self):
        self.killed = True


def make_client(tmp_path, proc):
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e))
    cfg = RunnerConfig(repo_dir=tmp_path, events=bus)
    client = CopilotClient(cfg, popen=lambda argv, **kw: proc)
    return client, events


def test_stream_mode_uses_json_output_format(tmp_path):
    captured = {}

    def popen(argv, **kw):
        captured["argv"] = argv
        return FakeProc(SCRIPT)

    bus = EventBus()
    cfg = RunnerConfig(repo_dir=tmp_path, events=bus)
    client = CopilotClient(cfg, popen=popen)
    client.run("q", role="builder.tester")
    assert "--output-format" in captured["argv"]
    assert "json" in captured["argv"]
    assert "-s" not in captured["argv"]


def test_stream_returns_final_message_content(tmp_path):
    client, _ = make_client(tmp_path, FakeProc(SCRIPT))
    reply = client.run("q", role="builder.tester")
    assert reply == '{"test_path": "tests/test_x.py"}'


def test_stream_emits_output_chunks_and_tool_lines(tmp_path):
    client, events = make_client(tmp_path, FakeProc(SCRIPT))
    client.run("q", role="builder.tester")
    chunks = [e.payload for e in events if e.kind == "agent_output"]
    joined = "".join(c["chunk"] for c in chunks)
    assert "thinking about it" in joined
    assert "bash" in joined  # tool line surfaced
    assert '"test_path"' in joined


def test_stream_finished_event_carries_usage(tmp_path):
    client, events = make_client(tmp_path, FakeProc(SCRIPT))
    client.run("q", role="builder.tester")
    finished = [e for e in events if e.kind == "agent_call_finished"][-1]
    assert finished.payload["usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_stream_nonzero_exit_raises(tmp_path):
    client, _ = make_client(tmp_path, FakeProc(SCRIPT, returncode=2))
    with pytest.raises(CopilotError):
        client.run("q", role="builder.tester")


def test_stream_garbage_lines_are_tolerated(tmp_path):
    noisy = "not json at all\n" + SCRIPT + "{broken\n"
    client, _ = make_client(tmp_path, FakeProc(noisy))
    assert client.run("q", role="planner") == '{"test_path": "tests/test_x.py"}'


def test_no_bus_keeps_legacy_path(tmp_path):
    # without an event bus the client must not require popen/json mode at all
    import subprocess

    def runner(argv, **kw):
        assert "-s" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="plain", stderr="")

    cfg = RunnerConfig(repo_dir=tmp_path)
    client = CopilotClient(cfg, runner=runner)
    assert client.run("q", role="planner") == "plain"
