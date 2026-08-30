import json
import subprocess

from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient
from issue_runner.events import EventBus
from issue_runner.orchestrator import run_issue
from tests.conftest import FakeClient
from tests.test_orchestrator import (
    ISSUE,
    git_repo,  # noqa: F401  (fixture reuse)
    implement,
    plan_reply,
    verdict,
    write_test,
)


def test_bus_delivers_to_all_subscribers():
    bus = EventBus()
    seen_a, seen_b = [], []
    bus.subscribe(lambda e: seen_a.append(e))
    bus.subscribe(lambda e: seen_b.append(e))
    bus.emit("phase", name="plan")
    assert seen_a[0].kind == "phase" and seen_a[0].payload == {"name": "plan"}
    assert seen_b == seen_a


def test_bus_subscriber_error_does_not_break_emit():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: 1 / 0)
    bus.subscribe(lambda e: seen.append(e))
    bus.emit("phase", name="plan")
    assert len(seen) == 1


def test_orchestrator_emits_lifecycle_events(git_repo, cfg):  # noqa: F811
    bus = EventBus()
    kinds = []
    bus.subscribe(lambda e: kinds.append(e.kind))
    cfg.events = bus
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1
    for expected in (
        "run_started",
        "phase",
        "tickets_updated",
        "ticket_started",
        "verdict",
        "ticket_done",
        "run_finished",
    ):
        assert expected in kinds, f"missing event {expected}; got {kinds}"


def test_copilot_client_emits_call_events(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e))

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    cfg = RunnerConfig(repo_dir=tmp_path, events=bus)
    client = CopilotClient(cfg, runner=runner)
    client.run("q", role="builder.coder")
    kinds = [e.kind for e in events]
    assert kinds[0] == "agent_call_started"
    assert kinds[-1] == "agent_call_finished"
    assert events[0].payload["role"] == "builder.coder"
    assert "elapsed" in events[-1].payload
