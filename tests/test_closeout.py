"""Phase 7: Dev checks after deployment, then the issue records the Definition of Done."""

import json
import sys
from pathlib import Path

import pytest

from issue_runner.cli import _exit_code
from issue_runner.criteria import parse
from issue_runner.phases import delivery
from tests.fake_github import Head
from tests.test_review_loop import (  # noqa: F401  (fixture reuse)
    env,
    git_repo,
    implement,
    run,
    verdict,
    write_test,
)

BODY = (
    "need it\n\n## Acceptance criteria\n\n"
    "- [ ] subtract works\n"
    "- [ ] the result shows in Dev\n\n"
    "## Notes\n\nnothing else\n"
)
ISSUE = {"number": 17, "title": "Add subtract", "body": BODY, "url": ""}


@pytest.fixture
def dod(env, tmp_path_factory, monkeypatch):  # noqa: F811
    fake, clock, cfg = env
    monkeypatch.setattr("tests.test_review_loop.ISSUE", ISSUE)
    cfg.preflight.criteria = parse(BODY)
    cfg.deploy_settings.dev_check_cmd = f"{sys.executable} {{check_path}}"
    cfg.deploy_settings.dev_check_ext = ".py"
    dev = tmp_path_factory.mktemp("dev")
    flag = dev / "DEPLOYED"
    fake.issues[17] = {"body": BODY, "state": "open", "state_reason": None}
    fake.on_deployed = lambda: flag.write_text("live")
    return fake, clock, cfg, flag


def check_source(flag, extra=""):
    return (
        "import os\n"
        f"{extra}"
        f"print('ACCEPT-PASS' if os.path.exists({str(flag)!r}) else 'ACCEPT-FAIL not live')\n"
    )


def script(repo, check):
    plan = {
        "summary": "one ticket",
        "criteria": [{"id": "AC1", "where": "tests"}, {"id": "AC2", "where": "dev"}],
        "tickets": [
            {
                "title": "subtract ints",
                "description": "implement subtract",
                "test_assertion": "subtract(5,3) == 2",
                "criteria": ["AC1", "AC2"],
            }
        ],
    }
    accept = {
        "criteria": [{"id": "AC1", "verdict": "met", "tests": ["test_sub.py"], "reason": "ok"}]
    }
    return [
        (json.dumps(plan), None),
        (json.dumps({"test_path": "test_sub.py"}), write_test(repo, "assert RED")),
        ("done", implement(repo)),
        (verdict("pass"), None),
        (json.dumps(accept), None),
        (json.dumps({"content": check}), None),
    ]


def test_the_definition_of_done_is_met_end_to_end(dod, git_repo):  # noqa: F811
    fake, _, cfg, flag = dod
    report, store, _ = run(cfg, git_repo, script(git_repo, check_source(flag)))
    d = store.delivery
    assert report.dod_met and _exit_code(report) == 0
    assert report.gates == dict.fromkeys(report.gates, "pass") and len(report.gates) == 6
    assert report.pr_url.endswith(f"/pull/{store.delivery.pr_number}")
    assert set(d.gates.values()) == {"pass"} and d.stage == "done" and d.closed_out
    assert d.criteria[1]["dev"] == "pass"
    issue = fake.issues[17]
    assert (issue["state"], issue["state_reason"]) == ("closed", "completed")
    assert "- [x] subtract works\n- [x] the result shows in Dev\n" in issue["body"]
    assert issue["body"].replace("[x]", "[ ]") == BODY
    [comment] = fake.issue_comments(17)
    assert comment.startswith("## Definition of Done: met")
    assert f"| merge | pass | `{d.merge_sha[:12]}` |" in comment
    assert "| criteria_dev | pass | AC2 |" in comment


def test_a_dev_check_that_still_fails_leaves_the_issue_open(dod, git_repo, monkeypatch):  # noqa: F811
    fake, clock, cfg, flag = dod
    fake.on_deployed = None  # the deploy succeeds, but Dev does not show the change
    start = clock[0]
    report, store, _ = run(cfg, git_repo, script(git_repo, check_source(flag)))
    d = store.delivery
    assert report.dod_failed_gate == "criteria_dev" and _exit_code(report) == 5
    assert d.failed_reason == "not met in Dev: AC2 (not live)"
    assert clock[0] - start >= 2 * delivery.DEV_CHECK_RETRY_SEC  # three attempts
    issue = fake.issues[17]
    assert issue["state"] == "open"
    assert "- [x] subtract works\n- [ ] the result shows in Dev\n" in issue["body"]
    [comment] = fake.issue_comments(17)
    assert comment.startswith("## Definition of Done: FAILED at criteria_dev")
    assert "| deploy | pass |" in comment and "| criteria_dev | FAILED |" in comment


def test_a_dev_check_that_passes_on_a_retry_counts(dod, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg, _ = dod
    fake.on_deployed = None
    counter = tmp_path_factory.mktemp("count") / "n"
    # fails before deployment (red) and on the first look after it, then passes
    flaky = (
        "import os\n"
        f"p = {str(counter)!r}\n"
        "n = int(open(p).read()) + 1 if os.path.exists(p) else 0\n"
        "open(p, 'w').write(str(n))\n"
        "print('ACCEPT-PASS' if n >= 2 else 'ACCEPT-FAIL cache still cold')\n"
    )
    report, store, _ = run(cfg, git_repo, script(git_repo, flaky))
    assert report.dod_met and store.delivery.criteria[1]["dev"] == "pass"


def test_a_dev_check_changed_after_acceptance_fails(dod, git_repo):  # noqa: F811
    fake, _, cfg, flag = dod
    fake.on_deployed = None

    def tamper():
        from issue_runner.tickets import TicketStore

        store = TicketStore(git_repo / ".state", issue_ref="17")
        store.load()
        path = store.delivery.criteria[1]["check_path"]
        Path(path).write_text("print('ACCEPT-PASS')\n")

    fake.on_deployed = tamper
    report, store, _ = run(cfg, git_repo, script(git_repo, check_source(flag)))
    assert report.dod_failed_gate == "criteria_dev"
    assert "changed after it was accepted" in store.delivery.failed_reason


def test_a_review_failure_is_recorded_on_the_issue_without_ticking(dod, git_repo):  # noqa: F811
    fake, _, cfg, flag = dod
    fake.scenarios = [Head(verdict=None)]
    report, _, _ = run(cfg, git_repo, script(git_repo, check_source(flag)))
    assert report.dod_failed_gate == "review"
    assert fake.issues[17]["body"] == BODY  # nothing is on main yet, so nothing is ticked
    [comment] = fake.issue_comments(17)
    assert comment.startswith("## Definition of Done: FAILED at review")
    assert "| merge | not reached |" in comment


def test_ticking_matches_criteria_by_text_on_the_current_issue_body(dod, git_repo):  # noqa: F811
    fake, _, cfg, flag = dod
    edited = BODY.replace(
        "## Acceptance criteria\n\n", "## Acceptance criteria\n\n- [ ] added later\n"
    )
    fake.issues[17]["body"] = edited
    report, _, _ = run(cfg, git_repo, script(git_repo, check_source(flag)))
    assert report.dod_met
    body = fake.issues[17]["body"]
    assert "- [ ] added later\n- [x] subtract works\n- [x] the result shows in Dev\n" in body
