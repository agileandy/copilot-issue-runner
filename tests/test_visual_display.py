"""The Python half of `--visual`: replay compaction and viewer control.

The rendering itself is a Bun app under apps/visual with its own frame tests;
what has to hold here is that every event reaches a viewer, that a reattached
viewer is given the state it missed, and that its controls drive the run.
"""

import json
from typing import ClassVar

import pytest

from issue_runner.control import RunControl
from issue_runner.events import RunEvent
from issue_runner.visual_display import (
    ReplayState,
    VisualUnavailable,
    ensure_viewer,
    run_visual,
    viewer_app_dir,
)


class Cfg:
    def __init__(self, repo_dir):
        self.repo_dir = repo_dir
        self.events = None
        self.control = RunControl()


def kinds(events):
    return [event["kind"] for event in events]


def payload_of(events, kind):
    return next(event["payload"] for event in events if event["kind"] == kind)


# -- replay compaction -----------------------------------------------------


def test_replay_carries_the_board_phase_and_totals_a_new_viewer_missed():
    replay = ReplayState()
    replay.record(RunEvent("run_started", {"issue_ref": "#19", "title": "tile the display"}))
    replay.record(RunEvent("phase", {"name": "build"}))
    replay.record(
        RunEvent("tickets_updated", {"tickets": [{"id": 1, "title": "one", "status": "done"}]})
    )
    replay.record(RunEvent("agent_call_started", {"role": "builder.coder"}))
    replay.record(
        RunEvent(
            "agent_call_finished",
            {"usage": {"input_tokens": 41000, "output_tokens": 2000, "nano_aiu": 7_000_000_000}},
        )
    )
    replay.record(RunEvent("agent_output", {"chunk": "writing the failing test"}))

    events = replay.replay()
    assert kinds(events) == [
        "run_started",
        "phase",
        "tickets_updated",
        "stats_snapshot",
        "agent_output",
    ]
    assert payload_of(events, "phase")["name"] == "build"
    stats = payload_of(events, "stats_snapshot")
    assert stats["calls"] == 1
    assert stats["input_tokens"] == 41000
    assert stats["nano_aiu"] == 7_000_000_000
    assert stats["unknown_cost_calls"] == 0
    assert stats["run_elapsed"] >= 0
    assert payload_of(events, "agent_output")["chunk"] == "writing the failing test"


def test_replay_counts_a_call_whose_cost_was_never_reported():
    replay = ReplayState()
    replay.record(RunEvent("agent_call_started", {"role": "planner"}))
    replay.record(RunEvent("agent_call_finished", {"usage": {"input_tokens": 5}}))
    assert payload_of(replay.replay(), "stats_snapshot")["unknown_cost_calls"] == 1


def test_replay_keeps_a_bounded_tail_of_the_output():
    replay = ReplayState()
    for index in range(4000):
        replay.record(RunEvent("agent_output", {"chunk": f"line-{index}\n"}))
    tail = payload_of(replay.replay(), "agent_output")["chunk"]
    assert len(tail) <= 20_000 + len("line-3999\n")
    assert tail.endswith("line-3999\n")
    assert "line-0\n" not in tail


def test_replay_reports_a_finished_run_and_a_pending_stop():
    replay = ReplayState()
    replay.record(RunEvent("stop_requested", {"message": "Stopping and cleaning up..."}))
    replay.record(RunEvent("run_finished", {"done": 1, "blocked": 0, "branch": "b"}))
    events = replay.replay()
    assert payload_of(events, "phase")["name"] == "finished"
    assert payload_of(events, "stop_requested")["message"].startswith("Stopping")
    assert payload_of(events, "run_finished")["done"] == 1
    assert replay.finished is not None


def test_a_blocked_ticket_reason_is_kept_in_full_for_the_output_pane():
    replay = ReplayState()
    reason = "failure details " * 30 + "END-OF-FAILURE"
    replay.record(RunEvent("ticket_blocked", {"ticket_id": 3, "reason": reason}))
    assert reason in payload_of(replay.replay(), "agent_output")["chunk"]


# -- availability ----------------------------------------------------------


def test_the_display_ships_with_the_package():
    assert (viewer_app_dir() / "src" / "index.ts").is_file()


def test_a_missing_bun_is_reported_as_unavailable_not_as_a_crash(monkeypatch):
    monkeypatch.setattr("issue_runner.visual_display.shutil.which", lambda name: None)
    with pytest.raises(VisualUnavailable, match="bun"):
        ensure_viewer(viewer_app_dir())


def test_a_missing_display_app_is_reported_as_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("issue_runner.visual_display.shutil.which", lambda name: "/usr/bin/bun")
    with pytest.raises(VisualUnavailable, match="no visual display"):
        ensure_viewer(tmp_path)


# -- the viewer loop -------------------------------------------------------


class FakeSession:
    """Stands in for the Bun process: records what it was sent, replays controls."""

    instances: ClassVar[list["FakeSession"]] = []
    scripts: ClassVar[list[list[str]]] = []

    def __init__(self, app_dir, bun, log_path=None):
        self.sent: list[dict] = []
        self.replayed: list[dict] = []
        self.script = FakeSession.scripts.pop(0) if FakeSession.scripts else []
        FakeSession.instances.append(self)

    def start(self, replay):
        self.replayed = list(replay)

    def send(self, event):
        # the wire is JSON: anything that cannot be encoded would be lost
        self.sent.append(json.loads(json.dumps({"kind": event.kind, "payload": event.payload})))

    def controls(self):
        return [self.script.pop(0)] if self.script else []

    def alive(self):
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_viewer(monkeypatch):
    FakeSession.instances = []
    FakeSession.scripts = []
    monkeypatch.setattr("issue_runner.visual_display.ensure_viewer", lambda app_dir: "bun")
    monkeypatch.setattr("issue_runner.visual_display.ViewerSession", FakeSession)
    return FakeSession


def test_every_event_reaches_the_viewer_and_the_run_is_reported(fake_viewer, monkeypatch, tmp_path):
    from issue_runner import orchestrator

    def run(cfg, client, issue, plan_only=False):
        cfg.events.emit("phase", name="build")
        cfg.events.emit("run_finished", done=2, blocked=0, branch="issue-1-x")
        return {"report": "ok"}

    monkeypatch.setattr(orchestrator, "run_issue", run)
    fake_viewer.scripts = [["closed"]]

    report, error, detached = run_visual(Cfg(tmp_path), object(), {"number": 1, "title": "t"})

    assert (report, error, detached) == ({"report": "ok"}, None, False)
    sent = kinds(fake_viewer.instances[0].sent)
    assert "phase" in sent and "run_finished" in sent


def test_the_viewer_stop_control_asks_the_run_to_stop(fake_viewer, monkeypatch, tmp_path):
    from issue_runner import orchestrator

    cfg = Cfg(tmp_path)
    stopped = []

    def run(c, client, issue, plan_only=False):
        for _ in range(200):
            if c.control.requested:
                stopped.append(True)
                break
            import time

            time.sleep(0.01)
        c.events.emit("run_finished", done=0, blocked=0, branch="")
        return {"report": "stopped"}

    monkeypatch.setattr(orchestrator, "run_issue", run)
    fake_viewer.scripts = [["stop", "closed"]]

    run_visual(cfg, object(), {"number": 1, "title": "t"})

    assert stopped == [True]
    assert cfg.control.requested


def test_detaching_then_reattaching_hands_the_new_viewer_the_state_it_missed(
    fake_viewer, monkeypatch, tmp_path, capsys
):
    from issue_runner import orchestrator, visual_display

    release = {"go": False}

    def run(cfg, client, issue, plan_only=False):
        cfg.events.emit("tickets_updated", tickets=[{"id": 1, "title": "one", "status": "done"}])
        cfg.events.emit("agent_call_started", role="planner")
        while not release["go"]:
            import time

            time.sleep(0.01)
        cfg.events.emit("run_finished", done=1, blocked=0, branch="issue-1-x")
        return {"report": "ok"}

    monkeypatch.setattr(orchestrator, "run_issue", run)

    keys = iter(["", "", "r"])

    def next_key():
        key = next(keys, "r")
        if key == "r":
            release["go"] = True
        return key or None

    monkeypatch.setattr(visual_display, "_terminal_key", next_key)
    fake_viewer.scripts = [["detach"], ["closed"]]

    report, error, _detached = run_visual(Cfg(tmp_path), object(), {"number": 1, "title": "t"})

    assert (report, error) == ({"report": "ok"}, None)
    assert len(fake_viewer.instances) == 2, "reattaching must start a second viewer"
    replayed = fake_viewer.instances[1].replayed
    assert payload_of(replayed, "tickets_updated")["tickets"][0]["title"] == "one"
    assert payload_of(replayed, "stats_snapshot")["calls"] == 1
    assert "display detached" in capsys.readouterr().out


def test_a_crash_still_settles_the_display_and_is_reported(fake_viewer, monkeypatch, tmp_path):
    from issue_runner import orchestrator

    def boom(cfg, client, issue, plan_only=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(orchestrator, "run_issue", boom)
    fake_viewer.scripts = [["closed"]]

    report, error, _detached = run_visual(Cfg(tmp_path), object(), {"number": 1, "title": "t"})

    assert report is None
    assert isinstance(error, RuntimeError)
    finished = [event for event in fake_viewer.instances[0].sent if event["kind"] == "run_finished"]
    assert finished, "a crash must still emit run_finished so the display can settle"
    assert finished[0]["payload"]["error"] == "kaboom"


def test_a_viewer_that_dies_falls_back_to_the_detached_stream(
    fake_viewer, monkeypatch, tmp_path, capsys
):
    from issue_runner import orchestrator, visual_display

    class DeadSession(FakeSession):
        def alive(self):
            return False

    monkeypatch.setattr(visual_display, "ViewerSession", DeadSession)
    monkeypatch.setattr(visual_display, "_terminal_key", lambda: None)

    def run(cfg, client, issue, plan_only=False):
        cfg.events.emit("run_finished", done=0, blocked=0, branch="")
        return {"report": "ok"}

    monkeypatch.setattr(orchestrator, "run_issue", run)

    report, _error, detached = run_visual(Cfg(tmp_path), object(), {"number": 1, "title": "t"})

    assert report == {"report": "ok"}
    assert detached is True, "a dead viewer must leave the run visible on the plain stream"
    assert "display detached" in capsys.readouterr().out
