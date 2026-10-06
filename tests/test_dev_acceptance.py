"""Acceptance criteria before the PR: planner coverage and the checked acceptor."""

import json
import sys

import pytest

from issue_runner.criteria import parse
from issue_runner.orchestrator import run_issue
from issue_runner.phases import acceptance
from issue_runner.phases.acceptance import AcceptError
from issue_runner.phases.preflight import Preflight, PushTrigger
from issue_runner.tickets import Ticket, TicketStore
from tests.conftest import FakeClient
from tests.test_orchestrator import (
    git_repo,  # noqa: F401  (fixture reuse)
    implement,
    verdict,
    write_test,
)

ISSUE = {
    "number": 17,
    "title": "Add subtract",
    "body": "need it\n\n## Acceptance criteria\n\n- [ ] subtract works\n- [ ] shown in Dev\n",
    "url": "",
}


def check_source(flag):
    """A Dev check that passes only once `flag` exists, i.e. once "Dev" has the change."""
    return (
        "import os\n"
        "print('looking at Dev')\n"
        f"print('ACCEPT-PASS' if os.path.exists({str(flag)!r}) else 'ACCEPT-FAIL not deployed')\n"
    )


def red_check(cfg):
    return json.dumps({"content": check_source(cfg.repo_dir / "DEPLOYED")})


def deploy_cfg(cfg):
    cfg.deploy = True
    cfg.deploy_settings.dev_check_cmd = f"{sys.executable} {{check_path}}"
    cfg.deploy_settings.dev_check_ext = ".py"
    cfg.preflight = Preflight(
        repo="o/n",
        default_branch="main",
        merge_method="squash",
        review_source="ruleset",
        review_on_push=False,
        required_approvals=0,
        required_checks=[],
        workflow_path=".github/workflows/dev-deployment.yaml",
        trigger=PushTrigger(),
        environment="development",
        criteria=parse(ISSUE["body"]),
    )
    return cfg


def plan_reply(where_ac1="tests"):
    return json.dumps(
        {
            "summary": "one ticket",
            "criteria": [{"id": "AC1", "where": where_ac1}, {"id": "AC2", "where": "dev"}],
            "tickets": [
                {
                    "title": "subtract ints",
                    "description": "implement subtract",
                    "test_assertion": "subtract(5,3) == 2",
                    "criteria": ["AC1"],
                }
            ],
        }
    )


def accept_reply(v="met", tests=("test_sub.py",), reason="proven"):
    return json.dumps(
        {"criteria": [{"id": "AC1", "verdict": v, "tests": list(tests), "reason": reason}]}
    )


def built_ticket_script(repo, tester_effect=None):
    return [
        (json.dumps({"test_path": "test_sub.py"}), tester_effect or write_test(repo, "assert RED")),
        ("done", implement(repo)),
        (verdict("pass"), None),
    ]


def test_pre_pr_met_criteria_pass_the_gate_and_move_on(git_repo, cfg):  # noqa: F811
    cfg = deploy_cfg(cfg)
    client = FakeClient(
        [
            (plan_reply(), None),
            *built_ticket_script(git_repo),
            (accept_reply(), None),
            (red_check(cfg), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")

    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    d = store.delivery
    assert d.gates["criteria_tests"] == "pass"
    assert d.stage == "accepted"
    assert report.dod_failed_gate == "review"  # the next stage is not built yet
    ac1, ac2 = d.criteria
    assert (ac1["where"], ac1["tickets"], ac1["pre_merge"], ac1["evidence"]) == (
        "tests",
        [1],
        "met",
        ["test_sub.py"],
    )
    assert (ac2["where"], ac2["pre_merge"]) == ("dev", None)
    assert ac2["red_output"] == "not deployed" and ac2["check_hash"]
    acceptor = next(c for c in client.calls if c["role"] == "acceptor")
    assert acceptor["read_only"] and "AC1: subtract works" in acceptor["prompt"]
    assert "AC2" not in acceptor["prompt"].split("Judge each")[1]
    assert "subtract ints" in acceptor["prompt"] and "impl.py" in acceptor["prompt"]


def test_pre_pr_unmet_criteria_get_one_round_of_extra_tickets(git_repo, cfg):  # noqa: F811
    def fresh_red():
        # drop the committed impl so the second ticket's test is genuinely red
        (git_repo / "impl.py").unlink()
        (git_repo / "test_two.py").write_text("assert RED")

    extra = json.dumps(
        {
            "summary": "close AC1",
            "tickets": [
                {
                    "title": "handle negatives",
                    "description": "d",
                    "test_assertion": "subtract(1,3) == -2",
                    "depends_on": [1],
                    "criteria": ["AC1"],
                }
            ],
        }
    )
    client = FakeClient(
        [
            (plan_reply(), None),
            *built_ticket_script(git_repo),
            (accept_reply("unmet", (), "negatives are not handled"), None),
            (extra, None),
            (json.dumps({"test_path": "test_two.py"}), fresh_red),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
            (accept_reply("met", ("test_sub.py", "test_two.py")), None),
            (red_check(cfg), None),
        ]
    )
    report = run_issue(deploy_cfg(cfg), client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 2
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert [t.id for t in store.tickets] == [1, 2]
    assert store.tickets[1].criteria == ["AC1"] and store.tickets[1].status == "done"
    assert store.delivery.criteria[0]["tickets"] == [1, 2]
    assert store.delivery.gates["criteria_tests"] == "pass"
    assert store.delivery.acceptance_round == 1
    planner_calls = [c for c in client.calls if c["role"] == "planner"]
    assert "negatives are not handled" in planner_calls[1]["prompt"]
    assert "Number the new tickets from 2" in planner_calls[1]["prompt"]


def test_pre_pr_unmet_criteria_with_no_rounds_left_fail_with_exit_5(git_repo, cfg):  # noqa: F811
    from issue_runner.cli import _exit_code

    cfg.deploy_settings.max_acceptance_rounds = 0
    client = FakeClient(
        [
            (plan_reply(), None),
            *built_ticket_script(git_repo),
            (accept_reply("met", ("test_missing.py",)), None),
        ]
    )
    report = run_issue(deploy_cfg(cfg), client, ISSUE, state_dir=git_repo / ".state")
    assert report.dod_failed_gate == "criteria_tests" and _exit_code(report) == 5
    assert "AC1 (cited test test_missing.py does not exist)" in report.details[-1]
    assert [c["role"] for c in client.calls].count("planner") == 1
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert store.delivery.criteria[0]["pre_merge"] == "unmet"
    assert store.delivery.stage == "built"


def test_pre_pr_an_all_dev_plan_passes_without_an_acceptor_call(git_repo, cfg):  # noqa: F811
    plan = json.loads(plan_reply(where_ac1="dev"))
    plan["tickets"][0]["criteria"] = []
    cfg = deploy_cfg(cfg)
    client = FakeClient(
        [
            (json.dumps(plan), None),
            *built_ticket_script(git_repo),
            (red_check(cfg), None),
            (red_check(cfg), None),
        ]
    )
    run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    roles = [c["role"] for c in client.calls]
    assert "acceptor" not in roles and roles.count("acceptance.tester") == 2
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert store.delivery.gates["criteria_tests"] == "pass"


# --- the acceptor's verdict is held to its evidence ---------------------------


@pytest.fixture
def built(git_repo, cfg):  # noqa: F811
    (git_repo / "test_ok.py").write_text("assert PASS")
    (git_repo / "test_red.py").write_text("assert RED")
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.initial_head = "HEAD"
    store.tickets = [Ticket(id=1, title="t", description="d", test_assertion="a")]
    return cfg, store


CRITERIA = [{"id": "AC1", "text": "works"}]


def judge(cfg, store, *replies):
    client = FakeClient([(r, None) for r in replies])
    return acceptance.judge(client, cfg, ISSUE, store, CRITERIA), client


@pytest.mark.parametrize(
    "tests, verdict_after, reason",
    [
        (["test_ok.py"], "met", "proven"),
        (["test_ok.py::test_case"], "met", "proven"),
        (["test_missing.py"], "unmet", "does not exist"),
        (["test_red.py"], "unmet", "fails"),
        ([], "unmet", "cited no test"),
        (["../outside.py"], "unmet", "outside"),
    ],
)
def test_pre_pr_a_met_verdict_counts_only_with_passing_cited_tests(
    built, tests, verdict_after, reason
):
    cfg, store = built
    [result], _ = judge(cfg, store, accept_reply("met", tests))
    assert result["verdict"] == verdict_after
    assert reason in result["reason"]


def test_pre_pr_an_invalid_acceptor_reply_is_retried_once(built):
    cfg, store = built
    [result], client = judge(cfg, store, "not json", accept_reply("met", ["test_ok.py"]))
    assert result["verdict"] == "met"
    assert "YOUR PREVIOUS REPLY WAS INVALID" in client.calls[1]["prompt"]

    with pytest.raises(AcceptError, match="no verdict for AC1"):
        judge(cfg, store, json.dumps({"criteria": []}), json.dumps({"criteria": []}))


# --- Dev checks: written before merge, red first --------------------------------

DEV_CRITERION = {"id": "AC2", "text": "shown in Dev", "where": "dev"}


@pytest.fixture
def harness(built):
    cfg, store = built
    deploy_cfg(cfg)
    return cfg, store


def write_check(cfg, store, *contents):
    client = FakeClient([(json.dumps({"content": c}), None) for c in contents])
    return acceptance.write_red_check(client, cfg, ISSUE, store, dict(DEV_CRITERION)), client


def test_red_check_that_fails_against_dev_is_frozen_outside_the_worktree(harness):
    cfg, store = harness
    record, client = write_check(cfg, store, check_source(cfg.repo_dir / "DEPLOYED"))
    path = acceptance.check_dir(store) / "AC2.py"
    assert record["check_path"] == str(path) and path.is_file()
    assert record["red_output"] == "not deployed"
    prompt = client.calls[0]["prompt"]
    assert client.calls[0]["read_only"] and "AC2: shown in Dev" in prompt
    assert "{check_path}" in prompt  # the command the harness will run is shown as configured
    assert acceptance.frozen_check(dict(DEV_CRITERION, **record)) == path


def test_red_check_that_passes_before_deployment_is_rejected(harness):
    cfg, store = harness
    always = "print('ACCEPT-PASS')\n"
    record, client = write_check(cfg, store, always, check_source(cfg.repo_dir / "DEPLOYED"))
    assert record["red_output"] == "not deployed"
    assert "ACCEPT-PASS against Dev before the change is deployed" in client.calls[1]["prompt"]


@pytest.mark.parametrize(
    "content, accepted",
    [
        ("print('ACCEPT-PASS')\nprint('ACCEPT-FAIL the earlier line is not the verdict')\n", True),
        ("print('ACCEPT-FAIL x')\nprint('trailing noise')\n", False),
        ("import sys\nsys.stderr.write('ACCEPT-FAIL on stderr only\\n')\n", False),
    ],
)
def test_red_only_the_last_printed_line_is_the_verdict(harness, content, accepted):
    cfg, store = harness
    cfg.tester_retries = 0
    if accepted:
        record, _ = write_check(cfg, store, content)
        assert record["red_output"] == "the earlier line is not the verdict"
    else:
        with pytest.raises(AcceptError, match="gave no verdict"):
            write_check(cfg, store, content)


def test_red_check_that_always_passes_fails_and_leaves_no_file(harness):
    cfg, store = harness
    cfg.tester_retries = 1
    with pytest.raises(AcceptError, match="no failing-first Dev check for AC2"):
        write_check(cfg, store, "print('ACCEPT-PASS')\n", "print('ACCEPT-PASS')\n")
    assert not (acceptance.check_dir(store) / "AC2.py").exists()


def test_red_a_check_that_hangs_is_cut_off(harness):
    cfg, store = harness
    cfg.deploy_settings.dev_check_timeout_sec = 1
    result_path = acceptance.check_dir(store)
    result_path.mkdir(parents=True)
    hang = result_path / "hang.py"
    hang.write_text("import time\ntime.sleep(5)\n")
    result = acceptance.run_check(cfg, hang, "AC2")
    assert result.verdict is None and "timed out after 1s" in result.reason


def test_red_a_frozen_check_that_changed_or_vanished_is_refused(harness):
    cfg, store = harness
    record, _ = write_check(cfg, store, check_source(cfg.repo_dir / "DEPLOYED"))
    frozen = dict(DEV_CRITERION, **record)
    path = acceptance.check_dir(store) / "AC2.py"
    path.write_text("print('ACCEPT-PASS')\n")
    with pytest.raises(AcceptError, match="changed after it was accepted"):
        acceptance.frozen_check(frozen)
    path.unlink()
    with pytest.raises(AcceptError, match="missing"):
        acceptance.frozen_check(frozen)


@pytest.mark.parametrize(
    "cmd, url, message",
    [
        ("", "", "dev_check_cmd is not set"),
        ("run-check", "", "must contain {check_path}"),
        ("run-check {check_path} {dev_url}", "", "dev_url is not set"),
    ],
)
def test_red_harness_problems_are_named(cfg, cmd, url, message):
    cfg.deploy_settings.dev_check_cmd, cfg.deploy_settings.dev_url = cmd, url
    assert message in acceptance.harness_problem(cfg, [DEV_CRITERION])
    assert acceptance.harness_problem(cfg, [dict(DEV_CRITERION, where="tests")]) is None


def test_red_no_harness_fails_right_after_planning_before_any_ticket(git_repo, cfg):  # noqa: F811
    from issue_runner.cli import _exit_code

    cfg = deploy_cfg(cfg)
    cfg.deploy_settings.dev_check_cmd = ""
    client = FakeClient([(plan_reply(), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert [c["role"] for c in client.calls] == ["planner"]
    assert report.dod_failed_gate == "criteria_dev" and _exit_code(report) == 5
    assert "dev_check_cmd is not set" in report.details[-1]

    # fixing the configuration resumes from the saved plan
    cfg.deploy_settings.dev_check_cmd = f"{sys.executable} {{check_path}}"
    client = FakeClient(
        [*built_ticket_script(git_repo), (accept_reply(), None), (red_check(cfg), None)]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1 and report.dod_failed_gate == "review"
