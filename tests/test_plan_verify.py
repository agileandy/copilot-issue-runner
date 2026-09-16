import json

import pytest

from issue_runner.phases.plan import PlanError, plan_step
from issue_runner.phases.verify import Verdict, VerifyError, verify_step
from issue_runner.tickets import Ticket
from tests.conftest import FakeClient

ISSUE = {"number": 17, "title": "Add subtract", "body": "We need subtraction", "url": ""}

GOOD_PLAN = json.dumps(
    {
        "summary": "add subtract fn",
        "tickets": [
            {
                "title": "subtract two ints",
                "description": "implement subtract()",
                "test_assertion": "subtract(5, 3) == 2",
                "files_hint": ["src/calc.py"],
            }
        ],
    }
)


def test_plan_step_returns_tickets(cfg):
    client = FakeClient([(GOOD_PLAN, None)])
    summary, tickets = plan_step(client, cfg, ISSUE)
    assert summary == "add subtract fn"
    assert len(tickets) == 1
    assert tickets[0].id == 1
    assert tickets[0].test_assertion == "subtract(5, 3) == 2"
    # planning must be a read-only exploration
    assert client.calls[0]["read_only"] is True
    # the issue content must reach the model
    assert "We need subtraction" in client.calls[0]["prompt"]


def test_plan_step_retries_bad_json_then_fails(cfg):
    client = FakeClient([("not json", None), ("still not json", None)])
    with pytest.raises(PlanError):
        plan_step(client, cfg, ISSUE)


def test_plan_step_rejects_ticket_missing_assertion(cfg):
    bad = json.dumps({"summary": "s", "tickets": [{"title": "t", "description": "d"}]})
    client = FakeClient([(bad, None), (bad, None)])
    with pytest.raises(PlanError, match="test_assertion"):
        plan_step(client, cfg, ISSUE)


def _ticket():
    return Ticket(id=1, title="t", description="d", test_assertion="a == 1")


@pytest.mark.parametrize("verdict", ["pass", "refine_test", "rework_code"])
def test_verify_step_parses_verdicts(cfg, verdict):
    reply = json.dumps(
        {"verdict": verdict, "reasons": ["r"], "test_feedback": "tf", "code_feedback": "cf"}
    )
    client = FakeClient([(reply, None)])
    v = verify_step(client, cfg, _ticket(), "test_x.py")
    assert isinstance(v, Verdict)
    assert v.verdict == verdict
    assert v.test_feedback == "tf"
    assert client.calls[0]["read_only"] is True


def test_verify_step_rejects_unknown_verdict(cfg):
    reply = json.dumps({"verdict": "maybe"})
    client = FakeClient([(reply, None), (reply, None)])
    with pytest.raises(VerifyError):
        verify_step(client, cfg, _ticket(), "test_x.py")


def test_verify_step_retries_a_list_reply_instead_of_crashing(cfg):
    """Regression: a JSON list reply raised AttributeError and killed the run."""
    good = json.dumps({"verdict": "pass", "reasons": ["ok"]})
    client = FakeClient([('[{"verdict": "pass"}]', None), (good, None)])
    ticket = Ticket(id=1, title="t", description="d", test_assertion="a == 1")
    assert verify_step(client, cfg, ticket, "tests/test_t.py").verdict == "pass"
    assert len(client.calls) == 1, "a single-element list is a usable object"


def test_verify_step_reports_a_persistent_non_object_as_a_verify_error(cfg):
    client = FakeClient([("[1, 2, 3]", None), ("[1, 2, 3]", None)])
    ticket = Ticket(id=1, title="t", description="d", test_assertion="a == 1")
    with pytest.raises(VerifyError) as excinfo:
        verify_step(client, cfg, ticket, "tests/test_t.py")
    assert "attribute" not in str(excinfo.value).lower()
    assert len(client.calls) == 2, "the verifier must be given a second chance"


def test_plan_step_retries_a_list_reply_instead_of_crashing(cfg):
    client = FakeClient([("[1, 2, 3]", None), (GOOD_PLAN, None)])
    summary, tickets = plan_step(client, cfg, ISSUE)
    assert summary == "add subtract fn" and len(tickets) == 1
    assert "INVALID" in client.calls[1]["prompt"]


def test_plan_step_reports_a_persistent_non_object_as_a_plan_error(cfg):
    client = FakeClient([("[1, 2, 3]", None), ("[1, 2, 3]", None)])
    with pytest.raises(PlanError) as excinfo:
        plan_step(client, cfg, ISSUE)
    assert "attribute" not in str(excinfo.value).lower()
