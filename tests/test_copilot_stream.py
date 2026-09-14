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
    assert finished.payload["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "model_calls": 1,
        # copilot sent no copilotUsage block, so the charge is unknown, not zero
        "costed_model_calls": 0,
    }
    assert "nano_aiu" not in finished.payload["usage"]


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


# --- real payload shapes, captured from copilot 1.0.82 -----------------------
#
# The previous fixture invented {"usage": {"input_tokens", "output_tokens"}}.
# The binary actually reports OpenAI-style names, so the parser filtered
# everything out and every run recorded null tokens.

REAL_CALL_SUCCESS = {
    "modelCall": {"model": "claude-opus-5"},
    "responseChunk": {
        "usage": {
            "prompt_tokens": 32854,
            "completion_tokens": 4,
            "total_tokens": 32858,
            "prompt_tokens_details": {"cached_tokens": 11, "cache_creation_tokens": 32852},
        }
    },
    "responseUsage": {
        "prompt_tokens": 32854,
        "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 11, "cache_creation_tokens": 32852},
    },
    "copilotUsage": {
        "token_details": [{"token_type": "input", "token_count": 2}],
        "total_nano_aiu": 20543500000,
    },
}


def parse(kind, **data):
    from issue_runner.copilot import _parse_event_line

    return _parse_event_line(json.dumps({"type": kind, "data": data}))


def test_real_call_success_yields_tokens():
    _, _, _, usage = parse("model.model_call_success", **REAL_CALL_SUCCESS)
    assert usage["input_tokens"] == 32854
    assert usage["output_tokens"] == 4


def test_real_call_success_yields_actual_credits():
    _, _, _, usage = parse("model.model_call_success", **REAL_CALL_SUCCESS)
    assert usage["nano_aiu"] == 20543500000


def test_real_call_success_yields_cached_tokens():
    _, _, _, usage = parse("model.model_call_success", **REAL_CALL_SUCCESS)
    assert usage["cached_tokens"] == 11


def test_anthropic_style_token_names_are_still_accepted():
    _, _, _, usage = parse(
        "model.model_call_success",
        responseChunk={"usage": {"input_tokens": 7, "output_tokens": 3}},
    )
    assert (usage["input_tokens"], usage["output_tokens"]) == (7, 3)


def test_call_success_without_usage_reports_a_call_of_unknown_cost():
    """The event proves a model call happened; absent tokens are unknown, not zero."""
    usage = parse("model.model_call_success", responseChunk={})[3]
    assert usage == {"model_calls": 1, "costed_model_calls": 0}


def test_streamed_run_records_real_tokens_and_credits(tmp_path):
    script = (
        jl("assistant.message", messageId="m1", content="the reply")
        + json.dumps({"type": "model.model_call_success", "data": REAL_CALL_SUCCESS})
        + "\n"
    )
    client, _ = make_client(tmp_path, FakeProc(script))
    assert client.run("p", role="planner", session_name="planner") == "the reply"
    totals = client.usage.totals()
    assert totals["input_tokens"] == 32854
    assert totals["output_tokens"] == 4
    assert totals["nano_aiu"] == 20543500000
