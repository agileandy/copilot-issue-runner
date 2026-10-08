"""update_pr_branch merges origin/<base> into a PR branch and pushes it fast-forward."""

import subprocess
import sys

import pytest

from issue_runner.phases import merge
from tests.conftest import FakeClient
from tests.test_orchestrator import git_repo  # noqa: F401  (fixture reuse)

BRANCH = "feature/7-x"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_a_passing_test(repo):
    (repo / "test_ok.py").write_text("PASS\n")
    _git(repo, "add", "test_ok.py")
    _git(repo, "commit", "-q", "-m", "test: a passing case")


@pytest.fixture
def pr_branch(git_repo, tmp_path_factory):  # noqa: F811
    remote = tmp_path_factory.mktemp("origin") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(git_repo, "remote", "add", "origin", str(remote))
    _commit_a_passing_test(git_repo)
    _git(git_repo, "push", "-q", "-u", "origin", "main")
    _git(git_repo, "checkout", "-q", "-b", BRANCH)
    (git_repo / "feature.py").write_text("y = 2\n")
    _git(git_repo, "add", "feature.py")
    _git(git_repo, "commit", "-q", "-m", "feat: feature work")
    _git(git_repo, "push", "-q", "-u", "origin", BRANCH)

    clone = tmp_path_factory.mktemp("teammate") / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    (clone / "other.py").write_text("x = 1\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "feat: upstream change")
    _git(clone, "push", "-q", "origin", "main")
    return remote


def test_update_pr_branch_pushes_the_merge_of_base_to_the_pr_branch(pr_branch, cfg):
    merge.update_pr_branch(FakeClient([]), cfg, BRANCH, "main")

    subject = subprocess.run(
        ["git", "--git-dir", str(pr_branch), "log", "-1", "--format=%s", BRANCH],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subject == f"chore: merge origin/main into {BRANCH}"


def test_update_pr_branch_does_not_push_a_merge_that_fails_the_regression_suite(pr_branch, cfg):
    import dataclasses

    failing = dataclasses.replace(cfg, regression_cmd='python3 -c "import sys; sys.exit(1)"')
    before = _git(pr_branch, "--git-dir", str(pr_branch), "rev-parse", BRANCH).stdout.strip()

    with pytest.raises(merge.MergeError):
        merge.update_pr_branch(FakeClient([]), failing, BRANCH, "main")

    assert _git(pr_branch, "--git-dir", str(pr_branch), "rev-parse", BRANCH).stdout.strip() == before


def test_update_pr_branch_does_not_push_a_merge_whose_regression_suite_runs_no_test(
    pr_branch, git_repo, cfg  # noqa: F811
):
    import dataclasses
    import sys

    no_tests_cfg = dataclasses.replace(
        cfg, regression_cmd=f"{sys.executable} {git_repo / 'checker.py'} feature.py"
    )
    before = _git(pr_branch, "--git-dir", str(pr_branch), "rev-parse", BRANCH).stdout.strip()

    with pytest.raises(merge.MergeError, match="the regression suite did not run"):
        merge.update_pr_branch(FakeClient([]), no_tests_cfg, BRANCH, "main")

    assert _git(pr_branch, "--git-dir", str(pr_branch), "rev-parse", BRANCH).stdout.strip() == before


@pytest.fixture
def conflicting_pr_branch(git_repo, tmp_path_factory):  # noqa: F811
    remote = tmp_path_factory.mktemp("origin") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(git_repo, "remote", "add", "origin", str(remote))
    (git_repo / "impl.py").write_text("base\n")
    _git(git_repo, "add", "impl.py")
    _git(git_repo, "commit", "-q", "-m", "feat: base impl")
    _commit_a_passing_test(git_repo)
    _git(git_repo, "push", "-q", "-u", "origin", "main")
    _git(git_repo, "checkout", "-q", "-b", BRANCH)
    (git_repo / "impl.py").write_text("ours\n")
    _git(git_repo, "commit", "-q", "-am", "feat: our impl")
    _git(git_repo, "push", "-q", "-u", "origin", BRANCH)

    clone = tmp_path_factory.mktemp("teammate") / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    (clone / "impl.py").write_text("upstream\n")
    _git(clone, "commit", "-q", "-am", "feat: upstream impl")
    _git(clone, "push", "-q", "origin", "main")
    return remote


def test_update_pr_branch_pushes_the_resolvers_fix_for_a_conflicted_merge(
    conflicting_pr_branch, git_repo, cfg  # noqa: F811
):
    client = FakeClient(
        [('{"notes":"ok"}', lambda: (git_repo / "impl.py").write_text("ours\n# and upstream\n"))]
    )

    merge.update_pr_branch(client, cfg, BRANCH, "main")

    pushed = _git(
        conflicting_pr_branch, "--git-dir", str(conflicting_pr_branch), "show", f"{BRANCH}:impl.py"
    ).stdout
    assert pushed == "ours\n# and upstream\n"


def test_update_pr_branch_rejects_a_resolver_edit_outside_the_conflict(
    conflicting_pr_branch, git_repo, cfg  # noqa: F811
):
    import dataclasses

    (git_repo / "other.py").write_text("x = 1\n")
    _git(git_repo, "add", "other.py")
    _git(git_repo, "commit", "-q", "-m", "feat: other")
    _git(git_repo, "push", "-q", "origin", BRANCH)
    no_retries = dataclasses.replace(cfg, coder_retries=0)

    def resolve_and_stray():
        (git_repo / "impl.py").write_text("ours\n# and upstream\n")
        (git_repo / "other.py").write_text("x = 2\n")

    client = FakeClient([('{"notes":"ok"}', resolve_and_stray)])

    with pytest.raises(merge.MergeError, match="outside the conflict: other.py"):
        merge.update_pr_branch(client, no_retries, BRANCH, "main")


def test_update_pr_branch_rejects_a_staged_resolver_edit_outside_the_conflict(
    conflicting_pr_branch, git_repo, cfg  # noqa: F811
):
    import dataclasses

    (git_repo / "other.py").write_text("x = 1\n")
    _git(git_repo, "add", "other.py")
    _git(git_repo, "commit", "-q", "-m", "feat: other")
    _git(git_repo, "push", "-q", "origin", BRANCH)
    no_retries = dataclasses.replace(cfg, coder_retries=0)

    def resolve_and_stage_stray():
        (git_repo / "impl.py").write_text("ours\n# and upstream\n")
        (git_repo / "other.py").write_text("x = 2\n")
        _git(git_repo, "add", "other.py")

    client = FakeClient([('{"notes":"ok"}', resolve_and_stage_stray)])

    with pytest.raises(merge.MergeError, match="outside the conflict: other.py"):
        merge.update_pr_branch(client, no_retries, BRANCH, "main")


def test_update_pr_branch_refuses_the_push_when_origins_pr_branch_moved_and_changes_no_ref(
    conflicting_pr_branch, git_repo, cfg, tmp_path_factory  # noqa: F811
):
    from issue_runner.phases import devops

    head = devops.head_commit(git_repo)
    advanced = {}

    def resolve_while_origin_advances():
        (git_repo / "impl.py").write_text("ours\n# and upstream\n")
        clone = tmp_path_factory.mktemp("racer") / "clone"
        subprocess.run(["git", "clone", "-q", str(conflicting_pr_branch), str(clone)], check=True)
        _git(clone, "checkout", "-q", BRANCH)
        (clone / "racer.py").write_text("z = 3\n")
        _git(clone, "add", "racer.py")
        _git(clone, "commit", "-q", "-m", "feat: teammate pushes to the PR branch")
        _git(clone, "push", "-q", "origin", BRANCH)
        advanced["sha"] = _git(clone, "rev-parse", "HEAD").stdout.strip()

    client = FakeClient([('{"notes":"ok"}', resolve_while_origin_advances)])

    with pytest.raises(merge.MergeError, match="moved during the update"):
        merge.update_pr_branch(client, cfg, BRANCH, "main")

    remote_tip = _git(
        conflicting_pr_branch, "--git-dir", str(conflicting_pr_branch), "rev-parse", BRANCH
    ).stdout.strip()
    local_tip = _git(git_repo, "rev-parse", BRANCH).stdout.strip()
    assert (remote_tip, local_tip) == (advanced["sha"], head)


def test_update_pr_branch_aborts_the_merge_when_the_resolver_raises(
    conflicting_pr_branch, git_repo, cfg  # noqa: F811
):
    from issue_runner.copilot import CopilotError
    from issue_runner.phases import devops

    def boom():
        raise CopilotError("boom")

    with pytest.raises(CopilotError):
        merge.update_pr_branch(FakeClient([('{"notes":"ok"}', boom)]), cfg, BRANCH, "main")

    status = _git(git_repo, "status", "--porcelain").stdout
    assert (status, devops.merge_in_progress(git_repo)) == ("", False)


@pytest.fixture
def up_to_date_pr_branch(git_repo, tmp_path_factory):  # noqa: F811
    remote = tmp_path_factory.mktemp("origin") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(git_repo, "remote", "add", "origin", str(remote))
    _commit_a_passing_test(git_repo)
    _git(git_repo, "push", "-q", "-u", "origin", "main")
    _git(git_repo, "checkout", "-q", "-b", BRANCH)
    (git_repo / "feature.py").write_text("y = 2\n")
    _git(git_repo, "add", "feature.py")
    _git(git_repo, "commit", "-q", "-m", "feat: feature work")
    _git(git_repo, "push", "-q", "-u", "origin", BRANCH)
    return remote


def test_update_pr_branch_returns_the_head_unchanged_when_the_branch_already_contains_base(
    up_to_date_pr_branch, git_repo, cfg  # noqa: F811
):
    from issue_runner.phases import devops

    head = devops.head_commit(git_repo)

    assert merge.update_pr_branch(FakeClient([]), cfg, BRANCH, "main") == head


def test_update_pull_request_merges_base_into_an_open_pr_in_its_own_worktree(
    pr_branch, git_repo, cfg  # noqa: F811
):
    import importlib

    from issue_runner.github_flow import GitHubFlow
    from tests.fake_github import FakeGitHub

    try:
        update_pull_request = importlib.import_module(
            "issue_runner.phases.update_pr"
        ).update_pull_request
    except ModuleNotFoundError:
        update_pull_request = merge.update_pull_request

    _git(git_repo, "checkout", "-q", "main")
    fake = FakeGitHub("o/n")
    fake.remote = pr_branch
    flow = GitHubFlow("o/n", run=fake)
    number = flow.create_pull("feat: x", BRANCH, "main", "body")["number"]

    update_pull_request(cfg, FakeClient([]), flow, number, git_repo / ".state")

    subject = subprocess.run(
        ["git", "--git-dir", str(pr_branch), "log", "-1", "--format=%s", BRANCH],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subject == f"chore: merge origin/main into {BRANCH}"


def test_update_pull_request_refuses_a_closed_pr_before_creating_a_worktree(
    pr_branch, git_repo, cfg  # noqa: F811
):
    from issue_runner.github_flow import GitHubFlow
    from issue_runner.phases.update_pr import update_pull_request
    from tests.fake_github import FakeGitHub

    _git(git_repo, "checkout", "-q", "main")
    fake = FakeGitHub("o/n")
    fake.remote = pr_branch
    flow = GitHubFlow("o/n", run=fake)
    number = flow.create_pull("feat: x", BRANCH, "main", "body")["number"]
    fake.pulls[number]["state"] = "closed"

    with pytest.raises(merge.MergeError, match="is closed"):
        update_pull_request(cfg, FakeClient([]), flow, number, git_repo / ".state")
    assert (git_repo / ".state" / "worktrees" / f"pr-{number}").exists() is False


def test_update_pull_request_refuses_a_fork_pr_before_creating_a_worktree(
    pr_branch, git_repo, cfg  # noqa: F811
):
    from issue_runner.github_flow import GitHubFlow
    from issue_runner.phases.update_pr import update_pull_request
    from tests.fake_github import FakeGitHub

    _git(git_repo, "checkout", "-q", "main")
    fake = FakeGitHub("o/n")
    fake.remote = pr_branch
    flow = GitHubFlow("o/n", run=fake)
    number = flow.create_pull("feat: x", BRANCH, "main", "body")["number"]
    fake.pulls[number]["head"]["repo"] = {"full_name": "fork/n"}

    with pytest.raises(merge.MergeError, match="fork/n"):
        update_pull_request(cfg, FakeClient([]), flow, number, git_repo / ".state")
    assert (git_repo / ".state" / "worktrees" / f"pr-{number}").exists() is False


def test_update_pull_request_runs_the_conflict_resolver_inside_the_pr_worktree(
    conflicting_pr_branch, git_repo, cfg  # noqa: F811
):
    import dataclasses
    from pathlib import Path

    from issue_runner.github_flow import GitHubFlow
    from issue_runner.phases.update_pr import update_pull_request
    from tests.fake_github import FakeGitHub

    class RecordingClient:
        def __init__(self):
            self.config = dataclasses.replace(cfg)
            self.repo_dirs = []

        def run(self, prompt, role, read_only=False, session_name=None):
            self.repo_dirs.append(self.config.repo_dir)
            (Path(self.config.repo_dir) / "impl.py").write_text("ours\n# and upstream\n")
            return '{"notes":"ok"}'

    _git(git_repo, "checkout", "-q", "main")
    fake = FakeGitHub("o/n")
    fake.remote = conflicting_pr_branch
    flow = GitHubFlow("o/n", run=fake)
    number = flow.create_pull("feat: x", BRANCH, "main", "body")["number"]
    client = RecordingClient()

    update_pull_request(cfg, client, flow, number, git_repo / ".state")

    pushed = _git(
        conflicting_pr_branch, "--git-dir", str(conflicting_pr_branch), "show", f"{BRANCH}:impl.py"
    ).stdout
    assert (pushed, client.config.repo_dir) == ("ours\n# and upstream\n", cfg.repo_dir)


def test_update_pull_request_runs_setup_cmd_inside_the_pr_worktree(
    pr_branch, git_repo, cfg, tmp_path_factory  # noqa: F811
):
    import dataclasses

    from issue_runner.github_flow import GitHubFlow
    from issue_runner.phases.update_pr import update_pull_request
    from tests.fake_github import FakeGitHub

    _git(git_repo, "checkout", "-q", "main")
    fake = FakeGitHub("o/n")
    fake.remote = pr_branch
    flow = GitHubFlow("o/n", run=fake)
    number = flow.create_pull("feat: x", BRANCH, "main", "body")["number"]
    marker = tmp_path_factory.mktemp("setup") / "cwd"
    # One python -c argument with no shell operator. It is left unquoted on
    # purpose: run as plain argv it works, while a shell would reject the `(`.
    write_cwd = (
        f'__import__(\\"pathlib\\").Path(r\\"{marker}\\")'
        f'.write_text(__import__(\\"os\\").getcwd())'
    )

    update_pull_request(
        dataclasses.replace(cfg, setup_cmd=[f"{sys.executable} -c {write_cwd}"]),
        FakeClient([]),
        flow,
        number,
        git_repo / ".state",
    )

    assert marker.read_text().strip() == str(
        (git_repo / ".state" / "worktrees" / f"pr-{number}").resolve()
    )
