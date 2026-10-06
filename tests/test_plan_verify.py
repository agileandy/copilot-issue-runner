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


# --- acceptance criteria coverage (--deploy) ---------------------------------

CRITERIA_ISSUE = dict(ISSUE, body="x\n\n## Acceptance criteria\n\n- [ ] works\n- [ ] in Dev\n")


def criteria_plan(criteria=None, ticket_criteria=("AC1",), depends_on=()):
    return json.dumps(
        {
            "summary": "s",
            "criteria": criteria
            if criteria is not None
            else [{"id": "AC1", "where": "tests"}, {"id": "AC2", "where": "dev"}],
            "tickets": [
                {
                    "title": "t",
                    "description": "d",
                    "test_assertion": "a",
                    "criteria": list(ticket_criteria),
                    "depends_on": list(depends_on),
                }
            ],
        }
    )


def plan_criteria(cfg, *replies):
    from issue_runner.criteria import parse
    from issue_runner.phases.plan import plan_with_criteria

    client = FakeClient([(r, None) for r in replies])
    return plan_with_criteria(client, cfg, CRITERIA_ISSUE, parse(CRITERIA_ISSUE["body"])), client


def test_plan_with_criteria_maps_every_criterion(cfg):
    (_, tickets, where), client = plan_criteria(cfg, criteria_plan())
    assert where == {"AC1": "tests", "AC2": "dev"}
    assert tickets[0].criteria == ["AC1"]
    prompt = client.calls[0]["prompt"]
    assert "AC1: works" in prompt and "AC2: in Dev" in prompt and '"where"' in prompt


@pytest.mark.parametrize(
    "reply, message",
    [
        (criteria_plan(criteria=[{"id": "AC1", "where": "tests"}]), "does not cover criteria AC2"),
        (criteria_plan(ticket_criteria=()), "no ticket names them"),
        (criteria_plan(ticket_criteria=("AC9",)), "unknown criteria AC9"),
        (
            criteria_plan(criteria=[{"id": "AC1", "where": "tests"}] * 2),
            "listed twice",
        ),
        (
            criteria_plan(criteria=[{"id": "AC1", "where": "prod"}, {"id": "AC2", "where": "dev"}]),
            '"tests" or "dev"',
        ),
        (
            json.dumps(
                {
                    "summary": "s",
                    "tickets": [{"title": "t", "description": "d", "test_assertion": "a"}],
                }
            ),
            'no top-level "criteria"',
        ),
    ],
)
def test_plan_with_criteria_rejects_gaps_and_retries(cfg, reply, message):
    with pytest.raises(PlanError, match=message):
        plan_criteria(cfg, reply, reply)

    _, client = plan_criteria(cfg, reply, criteria_plan())
    assert len(client.calls) == 2
    retry = client.calls[1]["prompt"].split("YOUR PREVIOUS REPLY WAS INVALID")[1]
    assert message in retry


@pytest.mark.parametrize("depends_on", [[9], [1]])
def test_plan_rejects_dependencies_on_tickets_that_do_not_exist(cfg, depends_on):
    reply = criteria_plan(depends_on=depends_on)
    with pytest.raises(PlanError, match="not another ticket"):
        plan_criteria(cfg, reply, reply)


def test_plan_prompt_without_criteria_is_unchanged(cfg):
    from issue_runner.phases.plan import build_plan_prompt

    prompt = build_plan_prompt(ISSUE)
    assert "Acceptance criteria" not in prompt
    assert "Planning only.\n\nReply with ONLY this JSON" in prompt


def test_plan_extra_numbers_new_tickets_after_the_built_ones(cfg):
    from issue_runner.phases.plan import plan_extra

    built = [
        Ticket(id=1, title="a", description="d", test_assertion="x", criteria=["AC1"]),
        Ticket(id=2, title="b", description="d", test_assertion="y"),
    ]
    unmet = [{"id": "AC1", "text": "works", "reason": "edge case missing"}]
    reply = json.dumps(
        {
            "summary": "more",
            "tickets": [
                {
                    "title": "c",
                    "description": "d",
                    "test_assertion": "z",
                    "criteria": ["AC1"],
                    "depends_on": [2],
                }
            ],
        }
    )
    client = FakeClient([(reply, None)])
    _, tickets = plan_extra(client, cfg, CRITERIA_ISSUE, unmet, built, ["AC1", "AC2"])
    assert [(t.id, t.depends_on, t.criteria) for t in tickets] == [(3, [2], ["AC1"])]
    assert "AC1: works — edge case missing" in client.calls[0]["prompt"]

    no_cover = reply.replace('"criteria": ["AC1"]', '"criteria": []')
    client = FakeClient([(no_cover, None), (no_cover, None)])
    with pytest.raises(PlanError, match="no new ticket names criteria AC1"):
        plan_extra(client, cfg, CRITERIA_ISSUE, unmet, built, ["AC1", "AC2"])
