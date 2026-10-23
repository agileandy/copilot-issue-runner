import re
from pathlib import Path

import pytest

from issue_runner import plan_issue
from issue_runner.phases.plan import PlanError

ROOT = Path(__file__).parent.parent
FIXTURE = (Path(__file__).parent / "fixtures" / "plan_gh_issue.md").read_text()
TASK = re.compile(r"^- \[[ x]\] (.+?) — assert: (.+)$", re.MULTILINE)


def test_task_lines_become_tickets_in_plan_order():
    plan = plan_issue.parse(FIXTURE, title="Plan: Rate limiting")

    expected = TASK.findall(FIXTURE.split("## Validation Commands")[0])
    expected = [(a, b) for a, b in expected if not a.startswith("`")]
    assert [t.id for t in plan.tickets] == [1, 2, 3]
    assert [(t.title, t.test_assertion) for t in plan.tickets] == expected
    assert [t.plan_phase for t in plan.tickets] == [1, 1, 2]
    assert plan.summary == "from plan issue: Rate limiting"


def test_body_without_the_marker_is_refused():
    with pytest.raises(plan_issue.PlanIssueError, match=re.escape(plan_issue.MARKER)):
        plan_issue.parse("## Purpose")


def test_task_without_an_assertion_is_refused():
    body = FIXTURE.replace(
        "- [ ] Refill over time — assert: after one refill interval `TokenBucket(2).take()` returns True again",
        "- [ ] Refill over time",
    )

    with pytest.raises(plan_issue.PlanIssueError, match="Refill over time"):
        plan_issue.parse(body)


def test_planner_ticket_rules_apply():
    body = FIXTURE.replace(
        "the third request from one client to `search()` within a second gets status 429",
        "capture evidence on the deployed Dev stack that search returns 429",
    )

    with pytest.raises(PlanError, match="deployed or running stack"):
        plan_issue.parse(body)


def test_description_names_phase_and_task_group():
    plan = plan_issue.parse(FIXTURE)

    assert "Limiter" in plan.tickets[0].description
    assert "1. Bucket" in plan.tickets[0].description
    assert "token bucket per client" in plan.tickets[0].description


def test_ticked_task_is_done():
    body = FIXTURE.replace("- [ ] Add TokenBucket", "- [x] Add TokenBucket")

    plan = plan_issue.parse(body)

    assert [t.status for t in plan.tickets] == ["done", "pending", "pending"]


def test_gates_are_collected():
    plan = plan_issue.parse(FIXTURE)

    assert plan.phase_gates[1] == ["uv run pytest tests/test_limiter.py"]
    assert plan.phase_gates[2] == ["uv run pytest tests/test_search.py"]
    assert plan.final_gates == ["uv run pytest"]


def test_runner_markers_do_not_change_the_plan():
    marked = FIXTURE.replace(
        "- [ ] Add TokenBucket — assert: `TokenBucket(2).take()` returns False on the third call",
        "- [x] Add TokenBucket — assert: `TokenBucket(2).take()` returns False on the third call · #12",
    ).replace(
        "- [ ] Refill over time — assert: after one refill interval `TokenBucket(2).take()` returns True again",
        "- [ ] failed Refill over time — assert: after one refill interval `TokenBucket(2).take()` "
        "returns True again · #13 — failed: no clock seam",
    )

    plain, again = plan_issue.parse(FIXTURE), plan_issue.parse(marked)

    assert again.fingerprint == plain.fingerprint
    assert [t.title for t in again.tickets] == [t.title for t in plain.tickets]


def test_split_and_render_round_trip():
    line = "- [ ] wip Add X — assert: x is 1 · #7"

    assert plan_issue.render_line(plan_issue.split_line(line)) == line


def test_readme_example_parses():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("### Plans from a plan issue", 1)[1]
    example = re.search(r"```markdown\n(.*?)\n```\n", section, re.DOTALL).group(1)

    plan = plan_issue.parse(example)

    assert plan.tickets and plan.final_gates


def test_ticked_gates_are_reported_so_a_run_does_not_untick_them():
    body = FIXTURE.replace(
        "- [ ] `uv run pytest tests/test_limiter.py`", "- [x] `uv run pytest tests/test_limiter.py`"
    ).replace("- [ ] `uv run pytest` —", "- [x] `uv run pytest` —")

    plan = plan_issue.parse(body)

    assert plan.gates_ticked == {(1, "uv run pytest tests/test_limiter.py"), (None, "uv run pytest")}


def test_a_separator_quoted_in_code_does_not_split_the_task():
    body = FIXTURE.replace(
        "- [ ] Add TokenBucket — assert:",
        "- [ ] Add TokenBucket, not `x — assert: y` — assert:",
    )

    ticket = plan_issue.parse(body).tickets[0]

    assert ticket.title == "Add TokenBucket, not `x — assert: y`"
    assert ticket.test_assertion == "`TokenBucket(2).take()` returns False on the third call"


def test_an_empty_assertion_is_refused():
    body = FIXTURE.replace(
        "after one refill interval `TokenBucket(2).take()` returns True again", ""
    )

    with pytest.raises(plan_issue.PlanIssueError, match="Refill over time"):
        plan_issue.parse(body)


def test_a_whitespace_only_gate_command_is_refused():
    body = FIXTURE.replace("`uv run pytest tests/test_search.py`", "`   `")

    with pytest.raises(plan_issue.PlanIssueError, match="search enforces the limit"):
        plan_issue.parse(body)
