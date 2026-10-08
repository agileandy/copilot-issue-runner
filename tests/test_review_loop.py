"""Phase 4: the PR is opened, reviewed, fixed and re-reviewed until the review passes."""

import json
import re
import subprocess

import pytest

from issue_runner.cli import _exit_code
from issue_runner.criteria import parse
from issue_runner.github_flow import GithubError, GitHubFlow
from issue_runner.orchestrator import run_issue
from issue_runner.phases import delivery, review
from issue_runner.phases.preflight import Preflight, PushTrigger
from issue_runner.tickets import Delivery, TicketStore
from tests.conftest import FakeClient
from tests.fake_github import APPROVE, BOT, CHANGES, FakeGitHub, Head
from tests.test_orchestrator import (
    git_repo,  # noqa: F401  (fixture reuse)
    implement,
    verdict,
    write_test,
)

ISSUE = {
    "number": 17,
    "title": "Add subtract",
    "body": "need it\n\n## Acceptance criteria\n\n- [ ] subtract works\n",
    "url": "",
}


# --- the review rule ------------------------------------------------------------

HEAD = "a" * 40


def copilot(commit=HEAD, verdict_line=APPROVE, summary="ok"):
    return {
        "id": 1,
        "user": {"login": BOT, "type": "Bot"},
        "state": "COMMENTED",
        "commit_id": commit,
        "body": f"## Copilot review overview\n\n### {verdict_line}\n\n{summary}\n\n**Findings:** x",
    }


def human(name, state, rid=9):
    return {"id": rid, "user": {"login": name, "type": "User"}, "state": state, "body": "please"}


def thread(resolved=False):
    return {
        "id": "T",
        "isResolved": resolved,
        "path": "a.py",
        "line": 3,
        "comments": {
            "nodes": [{"author": {"login": "copilot-pull-request-reviewer"}, "body": "bug"}]
        },
    }


def evaluate(**overrides):
    args = {
        "head_sha": HEAD,
        "checks": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}],
        "statuses": [],
        "reviews": [copilot()],
        "threads": [],
        "bot": "copilot-pull-request-reviewer",
        "required_approvals": 0,
    }
    args.update(overrides)
    return review.evaluate(**args)


def test_review_passes_only_when_everything_is_green_and_approved():
    assert evaluate().state == "pass"


@pytest.mark.parametrize(
    "overrides, waiting",
    [
        ({"checks": [{"name": "ci", "status": "in_progress"}]}, "check ci"),
        (
            {"statuses": [{"context": "deploy-preview", "state": "pending"}]},
            "status deploy-preview",
        ),
        ({"reviews": []}, "review of aaaaaaaa"),
        ({"reviews": [copilot(commit="b" * 40)]}, "review of aaaaaaaa"),
        ({"required_approvals": 1}, "1 more approval"),
    ],
)
def test_review_waits_for_what_has_not_happened_yet(overrides, waiting):
    status = evaluate(**overrides)
    assert status.state == "pending"
    assert any(waiting in w for w in status.waiting_for)


@pytest.mark.parametrize(
    "overrides, kind, text",
    [
        (
            {"checks": [{"id": 4, "name": "ci", "status": "completed", "conclusion": "failure"}]},
            "check",
            "failure",
        ),
        (
            {"statuses": [{"context": "lint", "state": "failure", "description": "2 errors"}]},
            "status",
            "2 errors",
        ),
        (
            {"reviews": [copilot(verdict_line=CHANGES, summary="the cache leaks")]},
            "review",
            "Changes recommended: the cache leaks",
        ),
        (
            {"reviews": [copilot(verdict_line="🔵 Needs a closer look", summary="risky")]},
            "review",
            "Needs a closer look: risky",
        ),
        ({"threads": [thread()]}, "thread", "bug"),
        (
            {"reviews": [copilot(), human("ann", "CHANGES_REQUESTED")]},
            "changes_requested",
            "please",
        ),
    ],
)
def test_review_findings_name_what_must_change(overrides, kind, text):
    status = evaluate(**overrides)
    assert status.state == "findings"
    assert [f.kind for f in status.findings] == [kind]
    assert text in status.findings[0].text


def test_review_resolved_threads_and_superseded_human_reviews_do_not_block():
    status = evaluate(
        threads=[thread(resolved=True)],
        reviews=[copilot(), human("ann", "CHANGES_REQUESTED", 1), human("ann", "APPROVED", 2)],
        required_approvals=1,
    )
    assert status.state == "pass"


def test_review_without_an_overview_heading_relies_on_threads():
    assert evaluate(reviews=[dict(copilot(), body="Looks good to me")]).state == "pass"


def test_update_pull_body_replaces_the_pr_description(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True)
    git = ["git", "--git-dir", str(remote), "-c", "user.name=t", "-c", "user.email=t@x"]
    tree = subprocess.run(
        [*git, "hash-object", "-t", "tree", "-w", "--stdin"],
        input="",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    commit = subprocess.run(
        [*git, "commit-tree", tree, "-m", "init"], capture_output=True, text=True, check=True
    ).stdout.strip()
    for branch in ("main", "b"):
        subprocess.run([*git, "update-ref", f"refs/heads/{branch}", commit], check=True)
    fake = FakeGitHub()
    fake.remote = remote
    flow = GitHubFlow("o/n", run=fake)
    pr = flow.create_pull("t", "b", "main", "old")
    flow.update_pull_body(pr["number"], "new body")
    assert fake.pulls[pr["number"]]["body"] == "new body"


def test_pr_body_lists_each_recorded_review_round(tmp_path):
    store = TicketStore(tmp_path, "17")
    store.delivery = Delivery(
        review_rounds=[
            {
                "round": 1,
                "sha": "abc123def4567890",
                "fixed": [{"where": "impl.py:1", "reason": "None is handled"}],
                "notes": "subtract now rejects None",
            },
            {
                "round": 2,
                "sha": "fedcba9876543210",
                "fixed": [{"where": "impl.py:2", "reason": "zero is handled"}],
                "notes": "subtract now rejects zero",
            },
        ]
    )
    body = delivery.pr_body(ISSUE, store)
    first = (
        "### Review round 1 (`abc123def456`)\n"
        "- `impl.py:1`: None is handled\n"
        "Changed behaviour: subtract now rejects None"
    )
    second = (
        "### Review round 2 (`fedcba987654`)\n"
        "- `impl.py:2`: zero is handled\n"
        "Changed behaviour: subtract now rejects zero"
    )
    assert first in body and second in body and body.index(first) < body.index(second)


# --- the loop, end to end -----------------------------------------------------------


@pytest.fixture
def env(git_repo, cfg, monkeypatch, tmp_path_factory):  # noqa: F811
    outside = tmp_path_factory.mktemp("origin")  # the repo fixture is tmp_path itself
    remote = outside / "remote.git"
    for args, where in (
        (["git", "init", "--bare", "-b", "main", str(remote)], outside),
        (["git", "remote", "add", "origin", str(remote)], git_repo),
        (["git", "push", "-q", "origin", "main"], git_repo),
    ):
        subprocess.run(args, cwd=where, check=True, capture_output=True)
    fake = FakeGitHub()
    fake.remote = remote
    clock = [1000.0]
    monkeypatch.setattr(delivery, "_now", lambda: clock[0])
    monkeypatch.setattr(delivery, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(delivery, "make_flow", lambda cfg: GitHubFlow("o/n", run=fake))
    cfg.deploy = True
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
    return fake, clock, cfg


def built_through_acceptance(repo):
    plan = {
        "summary": "one ticket",
        "criteria": [{"id": "AC1", "where": "tests"}],
        "tickets": [
            {
                "title": "subtract ints",
                "description": "implement subtract",
                "test_assertion": "subtract(5,3) == 2",
                "criteria": ["AC1"],
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
    ]


def reviser(threads=(), effect=None, notes=""):
    reply = {
        "threads": [{"ref": r, "action": a, "reason": why} for r, a, why in threads],
        "notes": notes,
    }
    return (json.dumps(reply), effect)


def edit(repo, name="impl.py", content="code, reviewed"):
    return lambda: (repo / name).write_text(content)


def run(cfg, git_repo, script):  # noqa: F811
    client = FakeClient(script)
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    return report, store, client


def log_subjects(repo):
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_a_clean_first_review_passes_and_opens_a_proper_pr(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert store.delivery.gates["review"] == "pass"
    pr = fake.pulls[store.delivery.pr_number]
    assert pr["title"] == "feat: Add subtract" and pr["draft"] is False
    assert pr["body"].startswith("Refs #17") and "Closes" not in pr["body"]
    assert "| AC1 | subtract works | tests in tickets 1 | met |" in pr["body"]
    assert store.pr_url.endswith(f"/pull/{store.delivery.pr_number}")
    assert fake.review_requests == []  # the ruleset reviews a new PR by itself


def test_a_thread_is_fixed_pushed_replied_resolved_and_rereviewed(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(verdict=CHANGES, threads=[("impl.py", 1, "handle None")]), Head()]
    script = built_through_acceptance(git_repo) + [
        # T1 is the review's verdict, T2 its inline thread
        reviser([("T2", "fixed", "None is handled")], edit(git_repo)),
    ]
    _, store, client = run(cfg, git_repo, script)
    prompt = next(c for c in client.calls if c["role"] == "reviser")["prompt"]
    assert "T1 [review] copilot-pull-request-reviewer: Changes recommended" in prompt
    assert "T2 [thread] impl.py:1: copilot-pull-request-reviewer: handle None" in prompt
    assert store.delivery.gates["review"] == "pass" and store.delivery.review_round == 1
    assert log_subjects(git_repo)[0] == "fix(review): address review round 1"
    head = store.delivery.head_sha
    assert fake.remote_head(store.branch) == head == store.last_commit
    [reply] = fake.thread_replies["PRRT_1"]
    assert reply.startswith("Fixed: None is handled") and head[:12] in reply
    assert fake.threads["PRRT_1"]["isResolved"]
    assert fake.review_requests == [(store.delivery.pr_number, BOT)]


def test_a_fixed_round_is_recorded_in_the_pr_body_after_the_push(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(verdict=CHANGES, threads=[("impl.py", 1, "handle None")]), Head()]
    script = built_through_acceptance(git_repo) + [
        reviser(
            [("T2", "fixed", "None is handled")], edit(git_repo), notes="subtract now rejects None"
        ),
    ]
    _, store, _ = run(cfg, git_repo, script)
    assert (
        f"### Review round 1 (`{store.delivery.head_sha[:12]}`)\n"
        "- `impl.py:1`: None is handled\n"
        "Changed behaviour: subtract now rejects None"
    ) in fake.pulls[store.delivery.pr_number]["body"]


def test_a_failing_check_goes_to_the_reviser_with_its_details(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(checks=[("ci", "failure")]), Head()]
    script = built_through_acceptance(git_repo) + [reviser((), edit(git_repo))]
    _, store, client = run(cfg, git_repo, script)
    assert store.delivery.gates["review"] == "pass"
    prompt = next(c for c in client.calls if c["role"] == "reviser")["prompt"]
    assert "T1 [check] ci: failure: ci failure" in prompt
    assert "test_sub.py" in prompt  # the frozen tests are named


def test_a_reviser_that_edits_a_frozen_test_is_restored_and_retried(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(checks=[("ci", "failure")]), Head()]
    script = built_through_acceptance(git_repo) + [
        reviser((), edit(git_repo, "test_sub.py", "assert PASS")),
        reviser((), edit(git_repo)),
    ]
    _, store, client = run(cfg, git_repo, script)
    assert (git_repo / "test_sub.py").read_text() == "assert RED"
    retry = [c for c in client.calls if c["role"] == "reviser"][1]["prompt"]
    assert "you modified accepted test files test_sub.py" in retry
    assert store.delivery.gates["review"] == "pass"


def test_a_change_that_breaks_a_ticket_test_is_rejected(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(checks=[("ci", "failure")]), Head()]
    cfg.coder_retries = 0

    def breaks():
        (git_repo / "impl.py").unlink()  # the RED test needs impl.py

    script = built_through_acceptance(git_repo) + [reviser((), breaks)]
    report, store, _ = run(cfg, git_repo, script)
    assert report.dod_failed_gate == "review" and _exit_code(report) == 6
    assert "test_sub.py now fails" in store.delivery.failed_reason


def test_declining_a_comment_needs_a_second_reviewer(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(verdict=APPROVE, threads=[("impl.py", 1, "rename x")])]
    script = built_through_acceptance(git_repo) + [
        reviser([("T1", "not_applicable", "x is the domain term")]),
        (json.dumps({"agree": True, "reason": "x matches the glossary"}), None),
    ]
    _, store, client = run(cfg, git_repo, script)
    assert store.delivery.gates["review"] == "pass"
    assert fake.threads["PRRT_1"]["isResolved"]
    [reply] = fake.thread_replies["PRRT_1"]
    assert reply.startswith("Not changed: x is the domain term (a second reviewer agreed")
    assert fake.review_requests == []  # nothing was pushed, so nothing to re-review
    arbiter = next(c for c in client.calls if c["role"] == "verifier" and "declined" in c["prompt"])
    assert arbiter["read_only"]


def test_an_unagreed_decline_stays_open_until_the_rounds_run_out(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    cfg.deploy_settings.max_review_rounds = 1
    fake.scenarios = [Head(verdict=APPROVE, threads=[("impl.py", 1, "rename x")])]
    script = built_through_acceptance(git_repo) + [
        reviser([("T1", "not_applicable", "fine as is")]),
        (json.dumps({"agree": False, "reason": "the name is misleading"}), None),
    ]
    report, store, _ = run(cfg, git_repo, script)
    assert report.dod_failed_gate == "review" and _exit_code(report) == 6
    assert "1 finding(s) still open after 1 review round(s)" in store.delivery.failed_reason
    assert not fake.threads["PRRT_1"]["isResolved"]
    number, body = fake.pr_comments[-1]
    assert number == store.delivery.pr_number and "`impl.py:1`" in body


def test_no_review_within_the_timeout_fails_the_gate(env, git_repo):  # noqa: F811
    fake, clock, cfg = env
    fake.scenarios = [Head(verdict=None)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "review"
    assert (
        "waited 30 min for a copilot-pull-request-reviewer review" in store.delivery.failed_reason
    )
    assert clock[0] - 1000.0 >= 30 * 60


def test_a_slow_review_is_waited_for(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(polls_until_done=4)]
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert store.delivery.gates["review"] == "pass"
    [seen] = fake.heads.values()
    assert seen["polls"] >= 4  # the review was only there on the fourth look


def test_a_head_pushed_by_someone_else_stops_the_run(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.external_push = "f" * 40
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "review"
    assert "head moved to ffffffffffff outside the runner" in store.delivery.failed_reason


def test_a_rerun_reuses_the_open_pr_and_retries_the_review(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(verdict=None)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "review"
    number = store.delivery.pr_number

    # the review finally lands; re-running the same command picks it up
    fake.heads[store.delivery.head_sha]["head"].verdict = APPROVE
    report, store, _ = run(cfg, git_repo, [])
    assert store.delivery.gates["review"] == "pass"
    assert list(fake.pulls) == [number]


def test_a_fix_round_resumed_after_a_failed_reply_is_numbered_round_2(
    env, git_repo, monkeypatch  # noqa: F811
):
    fake, _, cfg = env
    fake.scenarios = [
        Head(verdict=CHANGES, threads=[("impl.py", 1, "handle None")]),
        Head(verdict=CHANGES, threads=[("impl.py", 2, "handle zero")]),
        Head(),
    ]
    original = GitHubFlow.reply_to_thread
    failures = [GithubError("reply failed")]

    def reply_once_failing(self, thread_id, body):
        if failures:
            raise failures.pop()
        return original(self, thread_id, body)

    monkeypatch.setattr(GitHubFlow, "reply_to_thread", reply_once_failing)
    script = built_through_acceptance(git_repo) + [
        reviser([("T2", "fixed", "None is handled")], edit(git_repo)),
    ]
    report, store, _ = run(cfg, git_repo, script)
    assert report.dod_failed_gate == "review"
    assert [r["round"] for r in store.delivery.review_rounds] == [1]
    assert store.delivery.review_round == 0

    # the unanswered thread from round 1 is still open, so the reviser sees it again
    resume = [
        reviser(
            [("T1", "fixed", "None is handled")], edit(git_repo, content="code, reviewed twice")
        )
    ]
    _, store, client = run(cfg, git_repo, resume)
    prompt = next(c for c in client.calls if c["role"] == "reviser")["prompt"]
    assert "T1 [thread] impl.py:1: copilot-pull-request-reviewer: handle None" in prompt
    assert re.findall(r"### Review round \d+", fake.pulls[store.delivery.pr_number]["body"]) == [
        "### Review round 1",
        "### Review round 2",
    ]


def test_two_fix_rounds_both_appear_in_the_final_pr_body(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [
        Head(verdict=CHANGES, threads=[("impl.py", 1, "handle None")]),
        Head(verdict=CHANGES, threads=[("impl.py", 2, "handle zero")]),
        Head(),
    ]
    script = built_through_acceptance(git_repo) + [
        reviser([("T2", "fixed", "None is handled")], edit(git_repo)),
        reviser(
            [("T2", "fixed", "zero is handled")], edit(git_repo, content="code, reviewed twice")
        ),
    ]
    _, store, client = run(cfg, git_repo, script)
    prompts = [c["prompt"] for c in client.calls if c["role"] == "reviser"]
    assert "T2 [thread] impl.py:2: copilot-pull-request-reviewer: handle zero" in prompts[1]
    assert re.findall(r"### Review round \d+", fake.pulls[store.delivery.pr_number]["body"]) == [
        "### Review round 1",
        "### Review round 2",
    ]
