"""Phase 6: the merge's Dev deployment is followed until Dev reports it live."""

from issue_runner.cli import _exit_code
from issue_runner.phases.preflight import PushTrigger
from tests.fake_github import Deploy
from tests.test_review_loop import (  # noqa: F401  (fixture reuse)
    built_through_acceptance,
    env,
    git_repo,
    run,
)


def contains(fake, commit, tip):
    return fake._git("merge-base", "--is-ancestor", commit, tip, check=False).returncode == 0


def test_a_successful_deployment_of_the_merge_passes_the_gate(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    d = store.delivery
    assert d.gates["deploy"] == "pass"
    assert d.deployed_sha == d.merge_sha
    assert d.deployment_id == fake.deployment_list[0]["id"]
    assert d.deploy_run_ids == [fake.runs[0]["id"]]


def test_a_cancelled_run_superseded_by_a_newer_push_still_counts(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.deploys = [Deploy(run_conclusion="cancelled", deployment_state=None, supersede=Deploy())]
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    d = store.delivery
    assert d.gates["deploy"] == "pass"
    assert d.deployed_sha != d.merge_sha and contains(fake, d.merge_sha, d.deployed_sha)
    assert len(d.deploy_run_ids) == 2


def test_a_failed_deploy_run_fails_with_its_jobs_and_log(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.deploys = [Deploy(run_conclusion="failure", deployment_state=None)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "deploy" and _exit_code(report) == 8
    reason = store.delivery.failed_reason
    assert "deploy run 9001 failure (Deploy to Development / Deploy Application)" in reason
    assert "Error: the stack update failed" in reason
    number, body = fake.pr_comments[-1]
    assert number == store.delivery.pr_number and "the Dev deployment failed" in body


def test_a_deployment_that_reports_failure_fails_the_gate(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.deploys = [Deploy(deployment_state="failure")]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "deploy"
    assert "to development reported failure" in store.delivery.failed_reason


def test_a_merge_outside_the_paths_filter_fails_and_says_why(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    cfg.preflight.trigger = PushTrigger(branches=["main"], paths=["src/**"])
    fake.deploys = [Deploy(triggered=False)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "deploy" and _exit_code(report) == 8
    reason = store.delivery.failed_reason
    assert "did not start dev-deployment.yaml within 10 min" in reason
    assert "changed none of the workflow's paths (impl.py, test_sub.py)" in reason
    assert fake.dispatches == []


def test_dispatch_starts_the_deployment_when_enabled(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    cfg.deploy_settings.dispatch_if_not_triggered = True
    fake.deploys = [Deploy(triggered=False), Deploy()]
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert store.delivery.gates["deploy"] == "pass" and store.delivery.dispatched
    assert fake.dispatches == [{"workflow": "dev-deployment.yaml", "ref": "main"}]
    assert fake.runs[0]["event"] == "workflow_dispatch"


def test_a_deployment_that_never_finishes_times_out(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.deploys = [Deploy(polls=10**6)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "deploy"
    assert "no successful deployment of" in store.delivery.failed_reason
    assert "within 60 min (runs: 9001)" in store.delivery.failed_reason


def test_a_rerun_keeps_watching_and_never_dispatches_twice(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    cfg.deploy_settings.dispatch_if_not_triggered = True
    fake.deploys = [Deploy(triggered=False), Deploy(polls=10**6)]
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "deploy" and store.delivery.dispatched

    fake.runs[0]["_spec"].polls = 0  # the dispatched run finally finishes
    _, store, _ = run(cfg, git_repo, [])
    assert store.delivery.gates["deploy"] == "pass"
    assert len(fake.dispatches) == 1
