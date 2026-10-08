"""update_pr_branch merges origin/<base> into a PR branch and pushes it fast-forward."""

import subprocess

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


@pytest.fixture
def pr_branch(git_repo, tmp_path_factory):  # noqa: F811
    remote = tmp_path_factory.mktemp("origin") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(git_repo, "remote", "add", "origin", str(remote))
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
