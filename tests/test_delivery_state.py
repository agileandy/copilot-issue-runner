import json

import pytest

from issue_runner.budget import BudgetExhausted
from issue_runner.cli import _exit_code
from issue_runner.events import EventBus
from issue_runner.orchestrator import RunReport, run_issue
from issue_runner.phases import delivery
from issue_runner.tickets import Delivery, StateError, Ticket, TicketStore
from tests.conftest import FakeClient
from tests.test_orchestrator import (
    ISSUE,
    git_repo,  # noqa: F401  (fixture reuse)
    implement,
    plan_reply,
    verdict,
    write_test,
)


def test_delivery_round_trips_through_saved_state(tmp_path):
    store = TicketStore(tmp_path, "7")
    store.tickets = [Ticket(id=1, title="t", description="d", test_assertion="a")]
    store.delivery = Delivery(stage="merging", pr_number=9, handled_threads=["T1"])
    store.delivery.gates["review"] = "pass"
    store.save()
    assert json.loads(store.state_file.read_text())["version"] == 3

    loaded = TicketStore(tmp_path, "7")
    loaded.load()
    assert loaded.delivery == store.delivery
    assert loaded.delivery.gates["review"] == "pass"


def test_version_2_state_loads_without_a_delivery(tmp_path):
    (tmp_path / "issue-7.json").write_text(
        json.dumps({"version": 2, "issue_ref": "7", "tickets": []})
    )
    store = TicketStore(tmp_path, "7")
    assert store.load()
    assert store.delivery is None


@pytest.mark.parametrize(
    "raw", [{"stage": "teleporting"}, {"gates": {"vibes": "pass"}}, {"bogus": 1}]
)
def test_a_corrupt_delivery_record_refuses_to_resume(tmp_path, raw):
    (tmp_path / "issue-7.json").write_text(
        json.dumps({"version": 3, "issue_ref": "7", "tickets": [], "delivery": raw})
    )
    with pytest.raises(StateError, match="cannot resume"):
        TicketStore(tmp_path, "7").load()


def _run(cfg, tmp_path, handlers=None, monkeypatch=None):
    store = TicketStore(tmp_path, "7")
    report = RunReport()
    if handlers is not None:
        monkeypatch.setattr(delivery, "_HANDLERS", handlers)
    delivery.run(cfg, None, ISSUE, store, report)
    return store, report


def test_a_failed_stage_records_its_gate_and_maps_to_its_exit_code(cfg, tmp_path):
    store, report = _run(cfg, tmp_path)
    assert report.deploy and not report.dod_met
    assert report.dod_failed_gate == "criteria_tests"
    assert store.delivery.gates["tickets"] == "pass"
    assert store.delivery.gates["criteria_tests"] == "fail"
    assert "without acceptance criteria" in store.delivery.failed_reason
    assert _exit_code(report) == 5


@pytest.mark.parametrize(
    "gate, code", [("review", 6), ("merge", 7), ("deploy", 8), ("criteria_dev", 5)]
)
def test_each_gate_has_its_own_exit_code(gate, code):
    report = RunReport(deploy=True, dod_failed_gate=gate)
    assert _exit_code(report) == code


def test_exit_0_needs_the_definition_of_done():
    assert _exit_code(RunReport(deploy=True, dod_met=True)) == 0
    assert _exit_code(RunReport(deploy=True)) == 1
    assert _exit_code(RunReport()) == 0  # a run without --deploy is judged as before


def test_stages_advance_to_done_and_a_rerun_retries_the_failed_stage(cfg, tmp_path, monkeypatch):
    attempts = []

    def advance(to):
        def handler(cfg, client, issue, store, d):
            attempts.append(d.stage)
            d.stage = to

        return handler

    def flaky_review(cfg, client, issue, store, d):
        attempts.append(d.stage)
        if attempts.count("reviewing") == 1:
            raise delivery.GateFailed("copilot review timed out")
        d.stage = "merging"

    handlers = {
        "built": advance("accepted"),
        "accepted": advance("reviewing"),
        "reviewing": flaky_review,
        "revising": advance("reviewing"),
        "merging": advance("deploying"),
        "deploying": advance("verifying"),
        "verifying": advance("done"),
    }
    store, report = _run(cfg, tmp_path, handlers, monkeypatch)
    assert report.dod_failed_gate == "review" and _exit_code(report) == 6
    assert store.delivery.stage == "reviewing"

    store.load()
    report = RunReport()
    delivery.run(cfg, None, ISSUE, store, report)
    assert report.dod_met and _exit_code(report) == 0
    assert store.delivery.stage == "done" and store.delivery.failed_gate is None
    assert attempts == [
        "built",
        "accepted",
        "reviewing",
        "reviewing",
        "merging",
        "deploying",
        "verifying",
    ]


def test_a_budget_stop_pauses_delivery_and_keeps_the_stage(cfg, tmp_path, monkeypatch):
    def broke(*_):
        raise BudgetExhausted("credit budget spent")

    store, report = _run(cfg, tmp_path, dict.fromkeys(delivery.STAGE_GATE, broke), monkeypatch)
    assert report.budget_exhausted and _exit_code(report) == 4
    assert store.delivery.stage == "built" and store.delivery.failed_gate is None


def test_a_deploy_run_hands_built_tickets_to_delivery(git_repo, cfg):  # noqa: F811
    cfg.deploy = True
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
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
    assert report.done == 1 and not report.pr_url
    assert report.dod_failed_gate == "criteria_tests"
    finished = next(e for e in events if e.kind == "run_finished").payload
    assert finished["deploy"] is True
    assert finished["gates"]["tickets"] == "pass"
    assert finished["gates"]["criteria_tests"] == "fail"


def test_a_deploy_run_with_blocked_tickets_fails_the_tickets_gate(git_repo, cfg):  # noqa: F811
    from issue_runner.copilot import CopilotError

    class ExplodingClient(FakeClient):
        def run(self, prompt, role, read_only=False, session_name=None):
            if role == "builder.tester":
                raise CopilotError("copilot timed out")
            return super().run(prompt, role, read_only, session_name)

    cfg.deploy = True
    report = run_issue(
        cfg, ExplodingClient([(plan_reply(), None)]), ISSUE, state_dir=git_repo / ".state"
    )
    assert report.dod_failed_gate == "tickets"
    assert _exit_code(report) == 3


def test_the_deploy_pr_body_lists_remaining_pre_pr_findings(tmp_path):
    store = TicketStore(tmp_path, "7")
    ticket = Ticket(id=1, title="t", description="d", test_assertion="a")
    ticket.status = "done"
    store.tickets = [ticket]
    store.pre_pr_rounds = [
        {"round": 1, "findings": [{"command": "gh-code-quality", "text": "py/unused-import at a.py:3"}]}
    ]
    body = delivery.pr_body(ISSUE, store)
    assert "### Remaining pre-PR findings" in body and (
        "`gh-code-quality`: py/unused-import at a.py:3" in body
    )
