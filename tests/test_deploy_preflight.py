import pytest

from issue_runner.config import RunnerConfig
from issue_runner.github_flow import GitHubFlow
from issue_runner.phases import preflight
from issue_runner.phases.preflight import PreflightError, push_trigger
from tests.fake_github import WORKFLOW_PATH, FakeGitHub

ISSUE = {
    "number": 7,
    "title": "T",
    "body": "## Acceptance criteria\n\n- [ ] it works\n- [ ] in Dev it shows\n",
}


def check(fake, cfg=None, issue=ISSUE):
    cfg = cfg or RunnerConfig(repo_dir=".")
    return preflight.run(cfg, issue, GitHubFlow(fake.repo, run=fake))


def test_a_ready_repository_passes_and_records_its_policy():
    fake = FakeGitHub()
    fake.rules["main"].append(
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}}
    )
    result = check(fake)
    assert result.merge_method == "squash"
    assert (result.review_source, result.review_on_push) == ("ruleset", False)
    assert result.required_approvals == 1
    assert result.trigger.paths == ["src/**"]
    assert [c.id for c in result.criteria] == ["AC1", "AC2"]
    assert any("acceptance criteria: 2" in line for line in result.lines())
    assert fake.paths("POST") == [] and fake.paths("PUT") == []


def test_every_problem_is_reported_at_once():
    fake = FakeGitHub()
    fake.repo_data["permissions"]["push"] = False
    fake.repo_data["allow_merge_commit"] = True
    fake.workflows.clear()
    fake.environments.clear()
    with pytest.raises(PreflightError) as e:
        check(fake, issue={"number": 7, "title": "T", "body": "no criteria here"})
    text = "\n".join(e.value.problems)
    assert len(e.value.problems) == 5
    for needle in ("write access", "merge_method", "not found", "environment", "criteria"):
        assert needle in text


def test_an_explicit_merge_method_must_be_allowed():
    cfg = RunnerConfig(repo_dir=".")
    cfg.deploy_settings.merge_method = "rebase"
    with pytest.raises(PreflightError, match="rebase is not allowed"):
        check(FakeGitHub(), cfg)


def test_an_explicit_merge_method_picks_one_of_several():
    fake = FakeGitHub()
    fake.repo_data["allow_merge_commit"] = True
    cfg = RunnerConfig(repo_dir=".")
    cfg.deploy_settings.merge_method = "merge"
    assert check(fake, cfg).merge_method == "merge"


@pytest.mark.parametrize(
    "workflow, message",
    [
        ("on:\n  workflow_dispatch:\n", "does not run on push"),
        ("on:\n  push:\n    branches: [release]\n", "push to main"),
        ("name: x\njobs: {}\n", "cannot read the triggers"),
    ],
)
def test_a_deploy_workflow_that_a_merge_cannot_trigger_fails(workflow, message):
    fake = FakeGitHub()
    fake.files[(WORKFLOW_PATH, "main")] = workflow
    with pytest.raises(PreflightError, match=message):
        check(fake)


def test_a_disabled_workflow_fails():
    fake = FakeGitHub()
    fake.workflows["dev-deployment.yaml"]["state"] = "disabled_manually"
    with pytest.raises(PreflightError, match="disabled_manually"):
        check(fake)


def test_classic_protection_adds_approvals_and_checks_and_unreadable_protection_is_ignored():
    fake = FakeGitHub()
    fake.protection["main"] = {
        "required_pull_request_reviews": {"required_approving_review_count": 2},
        "required_status_checks": {"contexts": ["ci"]},
    }
    result = check(fake)
    assert (result.required_approvals, result.required_checks) == (2, ["ci"])

    fake = FakeGitHub()
    fake.errors["repos/o/n/branches/main/protection"] = "gh: Forbidden (HTTP 403)"
    assert check(fake).required_approvals == 0


def test_no_copilot_ruleset_means_the_runner_requests_the_review():
    fake = FakeGitHub()
    fake.rules["main"] = []
    result = check(fake)
    assert result.review_source == "request"
    assert any("requested by the runner" in line for line in result.lines())


def test_an_unreadable_repository_stops_immediately():
    fake = FakeGitHub()
    fake.errors["repos/o/n"] = "gh: Bad credentials (HTTP 401)"
    with pytest.raises(PreflightError, match="cannot read o/n"):
        check(fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "text, branches, paths",
    [
        ("on: push\n", None, None),
        ("on: [pull_request, push]  # both\n", None, None),
        ("'on':\n  push:\n    branches: ['main']\n", ["main"], None),
        (
            (
                "on:\n  pull_request:\n    paths: [x]\n  push:\n    # deploys\n    branches:\n"
                '      - main # only\n    paths:\n      - "a/**"\n      - b\n'
            ),
            ["main"],
            ["a/**", "b"],
        ),
    ],
)
def test_push_trigger_reads_common_shapes(text, branches, paths):
    trigger = push_trigger(text)
    assert (trigger.branches, trigger.paths) == (branches, paths)


def test_push_trigger_branch_filters():
    assert push_trigger("on:\n  push:\n    branches: ['release/**']\n").runs_on("release/1.2")
    ignored = push_trigger("on:\n  push:\n    branches-ignore: [main]\n")
    assert not ignored.runs_on("main") and ignored.runs_on("dev")


def test_push_trigger_rejects_what_it_cannot_read():
    assert push_trigger("on: pull_request\n") is None
    with pytest.raises(ValueError):
        push_trigger("on:\n  push: {branches: [main]}\n")
