"""Per-event usage accounting, proved through a real subprocess transport.

These tests do not stub the parser. They execute a small Python program that
speaks copilot's JSONL dialect over a real pipe, so what is exercised is the
transport the runner actually uses in production: `--output-format json` via
Popen, in headless and visual mode alike.

What the old code got wrong and these tests pin down:

- one CLI invocation makes several model calls; only the last one was counted;
- headless runs used `-s` and recorded nothing at all, so a run's real cost was
  invisible unless a TUI happened to be attached;
- an unreported charge was indistinguishable from a charge of zero;
- the run budget charged a flat estimate and never reconciled it against the
  figure copilot reported.
"""

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from issue_runner.budget import BudgetExhausted, RunBudget
from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient, CopilotError, measured_nano_aiu
from issue_runner.events import EventBus
from issue_runner.usage import merge_usage

# --- a deterministic stand-in that speaks copilot's event dialect -------------

FAKE_COPILOT = r'''#!/usr/bin/env python3
"""Deterministic copilot-shaped CLI. Mode comes from FAKE_MODE."""
import json
import os
import subprocess
import sys
import time


def send(kind, **data):
    sys.stdout.write(json.dumps({"type": kind, "data": data}) + "\n")
    sys.stdout.flush()


def call_success(prompt, completion, nano_aiu=None, cached=None):
    details = {"cached_tokens": cached} if cached is not None else {}
    data = {
        "responseUsage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_tokens_details": details,
        }
    }
    if nano_aiu is not None:
        data["copilotUsage"] = {"total_nano_aiu": nano_aiu}
    send("model.model_call_success", **data)


mode = os.environ.get("FAKE_MODE", "multi")

if mode == "multi":
    send("assistant.reasoning_delta", deltaContent="planning ")
    call_success(1000, 10, nano_aiu=1_000_000_000, cached=100)
    send("tool.execution_start", toolCallId="c1", toolName="bash", arguments={"command": "ls"})
    call_success(2000, 20, nano_aiu=2_500_000_000, cached=200)
    send("assistant.message_delta", messageId="m1", deltaContent="the ")
    send("assistant.message_delta", messageId="m1", deltaContent="answer")
    send("assistant.message", messageId="m1", content="the answer")
    call_success(3000, 30, nano_aiu=500_000_000)
elif mode == "zero":
    send("assistant.message", messageId="m1", content="free reply")
    call_success(5, 5, nano_aiu=0)
elif mode == "unknown":
    send("assistant.message", messageId="m1", content="uncosted reply")
    call_success(5, 5)
    call_success(6, 6)
elif mode == "partial":
    send("assistant.message", messageId="m1", content="half costed")
    call_success(5, 5, nano_aiu=7_000_000_000)
    call_success(6, 6)
elif mode == "stderr_flood":
    sys.stderr.write("x" * 400_000)
    sys.stderr.flush()
    send("assistant.message", messageId="m1", content="survived the flood")
    call_success(9, 9, nano_aiu=3_000_000_000)
elif mode == "empty":
    call_success(1, 0, nano_aiu=4_000_000_000)
elif mode == "fail":
    send("model.turn_failed", reason="the model gave up")
    call_success(1, 0, nano_aiu=6_000_000_000)
    sys.stderr.write("fatal: nope\n")
    raise SystemExit(2)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(os.environ["FAKE_PIDFILE"], "w") as fh:
        fh.write("%d %d\n" % (os.getpid(), child.pid))
    time.sleep(600)
'''


@pytest.fixture
def fake_copilot(tmp_path):
    path = tmp_path / "fake-copilot.py"
    path.write_text(FAKE_COPILOT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    shim = tmp_path / "fake-copilot"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{path}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def client_for(tmp_path, fake_copilot, mode, bus=None, **cfg_kwargs):
    os.environ["FAKE_MODE"] = mode
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        copilot_cmd=str(fake_copilot),
        events=bus,
        timeout=cfg_kwargs.pop("timeout", 20),
        **cfg_kwargs,
    )
    return CopilotClient(cfg)


@pytest.fixture(autouse=True)
def _clear_mode():
    yield
    os.environ.pop("FAKE_MODE", None)
    os.environ.pop("FAKE_PIDFILE", None)


# --- aggregation across every model call in one invocation -------------------


def test_every_model_call_in_one_invocation_is_summed(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "multi")
    assert client.run("p", role="planner", session_name="planner") == "the answer"

    totals = client.usage.totals()
    assert totals["calls"] == 1, "one CLI invocation is still one accounted call"
    assert totals["model_calls"] == 3, "all three model calls must be counted"
    assert totals["input_tokens"] == 6000
    assert totals["output_tokens"] == 60
    assert totals["cached_tokens"] == 300


def test_real_nano_aiu_charges_are_totalled_not_overwritten(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "multi")
    client.run("p", role="planner")
    # the last event alone charged 0.5 AIU; the invocation actually cost 4.0
    assert client.usage.totals()["nano_aiu"] == 4_000_000_000
    assert "4.00 AIU" in client.usage.summary_line()


def test_merge_usage_keeps_unknown_distinct_from_zero():
    merged = merge_usage(None, {"input_tokens": 5, "model_calls": 1, "costed_model_calls": 0})
    merged = merge_usage(merged, {"output_tokens": 2, "model_calls": 1, "costed_model_calls": 0})
    assert merged["input_tokens"] == 5
    assert merged["output_tokens"] == 2
    assert merged.get("nano_aiu") is None, "nobody reported a charge, so it is not zero"
    assert merged["model_calls"] == 2


# --- headless and visual use the same accounting -----------------------------


def test_headless_and_visual_record_identical_usage(tmp_path, fake_copilot):
    headless = client_for(tmp_path, fake_copilot, "multi")
    headless.run("p", role="planner")

    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    visual = client_for(tmp_path, fake_copilot, "multi", bus=bus)
    visual.run("p", role="planner")

    assert headless.usage.totals() == visual.usage.totals()
    assert headless.usage.totals()["nano_aiu"] == 4_000_000_000
    assert headless.budget.measured_nano_aiu == visual.budget.measured_nano_aiu


def test_headless_uses_the_json_transport_not_unmetered_dash_s(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "multi")
    argv = client._build_argv("p", "planner", False, None, structured=not client.plain_transport)
    assert "--output-format" in argv and "json" in argv
    assert "-s" not in argv


def test_headless_emits_no_chunks_but_visual_does(tmp_path, fake_copilot):
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    client_for(tmp_path, fake_copilot, "multi", bus=bus).run("p", role="planner")
    chunks = [e for e in seen if e.kind == "agent_output"]
    assert chunks, "a subscriber must still receive streaming chunks"
    assert "planning" in "".join(e.payload["chunk"] for e in chunks)

    # headless has no bus at all, so nothing is rendered and nothing is buffered
    headless = client_for(tmp_path, fake_copilot, "multi")
    assert headless.config.events is None
    assert headless.run("p", role="planner") == "the answer"


def test_an_injected_runner_still_overrides_the_transport(tmp_path):
    """The test seam must survive: production never takes it, tests still can."""
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="injected", stderr="")

    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=runner)
    assert client.run("p", role="planner") == "injected"
    assert "-s" in calls[0]


# --- zero vs unknown ---------------------------------------------------------


def test_a_genuine_zero_charge_is_reported_as_zero(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "zero")
    assert client.run("p", role="planner") == "free reply"
    assert client.usage.totals()["nano_aiu"] == 0
    assert "0.00 AIU" in client.usage.summary_line()
    assert client.budget.measured_calls == 1
    assert client.budget.unknown_calls == 0


def test_a_zero_charge_releases_the_reservation(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "zero", max_ai_credits=5, max_run_credits=10)
    for _ in range(6):
        client.run("p", role="planner")
    assert client.budget.reserved == 0, "measured zero must not hold an estimate"
    assert client.budget.spent == 0
    assert client.budget.calls == 6


def test_an_unreported_charge_stays_unknown_never_zero(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "unknown")
    assert client.run("p", role="planner") == "uncosted reply"
    totals = client.usage.totals()
    assert totals["nano_aiu"] is None, "unknown must not masquerade as free"
    assert totals["model_calls"] == 2
    assert totals["input_tokens"] == 11
    assert "credits: unknown" in client.usage.summary_line()
    assert client.budget.unknown_calls == 1
    assert client.budget.measured_calls == 0


def test_partly_costed_invocations_are_not_treated_as_measured(tmp_path, fake_copilot):
    """One of two model calls reported a charge: that total is not a measurement."""
    client = client_for(tmp_path, fake_copilot, "partial", max_ai_credits=3)
    client.run("p", role="planner")
    usage = client.usage.calls[0]
    assert usage.nano_aiu == 7_000_000_000, "the partial figure is still recorded"
    assert client.budget.unknown_calls == 1
    assert client.budget.measured_calls == 0
    assert client.budget.reserved == 3, "the estimate stands while the cost is incomplete"


def test_measured_nano_aiu_rejects_partial_totals():
    assert measured_nano_aiu(None) is None
    assert measured_nano_aiu({"model_calls": 2, "costed_model_calls": 1, "nano_aiu": 5}) is None
    assert measured_nano_aiu({"model_calls": 2, "costed_model_calls": 2, "nano_aiu": 5}) == 5
    assert measured_nano_aiu({"model_calls": 1, "costed_model_calls": 1, "nano_aiu": 0}) == 0


# --- budget reconciliation and the stop rule ---------------------------------


def test_budget_replaces_the_estimate_with_the_measured_charge(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "multi", max_ai_credits=10, max_run_credits=10)
    client.run("p", role="planner")
    assert client.budget.reserved == 0
    assert client.budget.measured_nano_aiu == 4_000_000_000
    assert client.budget.spent == 4
    assert client.budget.remaining == 6


def test_measured_overspend_stops_the_next_call(tmp_path, fake_copilot):
    """Each invocation really costs 4 AIU, four times the 1-credit reservation.

    An estimate-only budget would allow ten calls against a limit of 10; the
    measured charge stops the run after three.
    """
    client = client_for(tmp_path, fake_copilot, "multi", max_run_credits=10)
    for _ in range(3):
        client.run("p", role="planner")
    assert client.budget.measured_nano_aiu == 12_000_000_000
    assert client.budget.spent == 12
    with pytest.raises(BudgetExhausted):
        client.run("p", role="planner")
    assert client.budget.calls == 3, "the fourth call never reached the binary"


def test_unknown_cost_calls_still_consume_the_reservation(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "unknown", max_ai_credits=4, max_run_credits=8)
    client.run("p", role="planner")
    client.run("p", role="planner")
    assert client.budget.reserved == 8
    with pytest.raises(BudgetExhausted):
        client.run("p", role="planner")


def test_budget_message_does_not_claim_a_hard_guarantee():
    budget = RunBudget(limit=1, per_call=1)
    budget.charge()
    budget.settle(None)
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.check()
    message = str(excinfo.value)
    assert "not a guarantee" in message
    assert "unknown cost" in message


def test_budget_status_separates_reserved_from_measured():
    budget = RunBudget(limit=10, per_call=2)
    budget.charge()
    budget.settle(1_500_000_000)
    budget.charge()
    budget.settle(None)
    status = budget.status()
    assert status["measured_nano_aiu"] == 1_500_000_000
    assert status["measured_aiu"] == 1.5
    assert status["reserved_aiu"] == 2
    assert status["unknown_cost_calls"] == 1
    assert status["measured_calls"] == 1
    assert status["spent"] == 3.5
    assert "measured: 1.50 AIU" in budget.describe()


# --- failure, empty and timeout ----------------------------------------------


def test_an_empty_reply_is_retried_and_every_attempt_is_accounted(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "empty", empty_reply_retries=1)
    with pytest.raises(CopilotError, match="empty reply"):
        client.run("p", role="planner")
    totals = client.usage.totals()
    assert totals["calls"] == 2, "both wasted attempts really ran"
    assert totals["failed"] == 2
    assert totals["nano_aiu"] == 8_000_000_000, "a blank reply is not a free call"
    assert client.budget.calls == 2


def test_a_failed_turn_is_accounted_and_raises(tmp_path, fake_copilot):
    client = client_for(tmp_path, fake_copilot, "fail", empty_reply_retries=0)
    with pytest.raises(CopilotError, match="exited 2"):
        client.run("p", role="planner")
    totals = client.usage.totals()
    assert totals["calls"] == 1
    assert totals["failed"] == 1
    assert totals["nano_aiu"] == 6_000_000_000, "a failed turn still cost credit"


def test_a_timeout_raises_accounts_and_leaves_no_child_running(tmp_path, fake_copilot):
    pidfile = tmp_path / "pids"
    os.environ["FAKE_PIDFILE"] = str(pidfile)
    client = client_for(tmp_path, fake_copilot, "hang", timeout=2, empty_reply_retries=0)
    with pytest.raises(CopilotError, match="timed out"):
        client.run("p", role="planner")

    assert client.usage.totals()["failed"] == 1
    assert client.usage.totals()["nano_aiu"] is None
    assert client.budget.unknown_calls == 1, "a timeout is unknown cost, not zero"

    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2
    for pid in pids:
        assert _wait_for_exit(pid), f"process {pid} survived the timeout"


def _wait_for_exit(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.1)
    return False


def test_a_chatty_stderr_cannot_stall_stdout(tmp_path, fake_copilot):
    """400KB of stderr overflows the pipe buffer unless it is drained concurrently."""
    client = client_for(tmp_path, fake_copilot, "stderr_flood", timeout=20)
    assert client.run("p", role="planner") == "survived the flood"
    assert client.usage.totals()["nano_aiu"] == 3_000_000_000


# --- the bundled offline demo CLI, run for real ------------------------------


def test_the_demo_cli_reports_a_genuine_zero_cost_run(tmp_path, monkeypatch):
    """The demo makes no model call, so 0 AIU here is measured, not assumed."""
    from issue_runner.demo import setup_demo

    monkeypatch.setenv("ISSUE_RUNNER_DEMO_DELAY", "0")
    env = setup_demo(tmp_path / "sandbox")
    cfg = RunnerConfig(
        repo_dir=env.repo_dir,
        copilot_cmd=str(env.copilot_cmd),
        max_ai_credits=5,
        max_run_credits=20,
        timeout=60,
    )
    client = CopilotClient(cfg)
    reply = client.run("You are the planner in an automated TDD pipeline", role="planner")

    assert json.loads(reply)["tickets"], "the demo transport still returns a usable reply"
    totals = client.usage.totals()
    assert totals["nano_aiu"] == 0
    assert totals["model_calls"] == 1
    assert totals["input_tokens"] is not None
    assert client.budget.measured_calls == 1
    assert client.budget.reserved == 0, "a measured zero must not hold a 5-credit estimate"


def test_a_non_event_cli_still_replies_with_usage_marked_unknown(tmp_path):
    """A shim that does not speak JSONL must not be silently recorded as free."""
    shim = tmp_path / "plain-copilot"
    shim.write_text("#!/bin/sh\necho 'just some text'\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    cfg = RunnerConfig(repo_dir=tmp_path, copilot_cmd=str(shim), timeout=20)
    client = CopilotClient(cfg)
    assert client.run("p", role="planner") == "just some text"
    assert client.usage.totals()["nano_aiu"] is None
    assert client.budget.unknown_calls == 1
