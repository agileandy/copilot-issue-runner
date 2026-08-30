import subprocess

import pytest

from issue_runner.phases.devops import DevopsError, commit_ticket, create_branch, current_branch
from issue_runner.tickets import Ticket


@pytest.fixture
def git_repo(repo):
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "runner@test.local")
    git("config", "user.name", "Runner Test")
    (repo / "seed.txt").write_text("seed")
    git("add", "-A")
    git("commit", "-m", "seed")
    return repo


def _ticket():
    return Ticket(id=1, title="add subtract", description="d", test_assertion="a")


def test_create_branch_switches(git_repo):
    branch = create_branch(git_repo, "17", "add-subtract")
    assert branch == "issue-17-add-subtract"
    assert current_branch(git_repo) == "issue-17-add-subtract"


def test_create_branch_reuses_existing(git_repo):
    create_branch(git_repo, "17", "add-subtract")
    subprocess.run(["git", "switch", "main"], cwd=git_repo, check=True, capture_output=True)
    branch = create_branch(git_repo, "17", "add-subtract")
    assert branch == "issue-17-add-subtract"
    assert current_branch(git_repo) == "issue-17-add-subtract"


def test_commit_ticket_commits_changes(git_repo):
    create_branch(git_repo, "17", "add-subtract")
    (git_repo / "new.py").write_text("x = 1")
    sha = commit_ticket(git_repo, _ticket())
    assert sha
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "add subtract" in log


def test_commit_ticket_refuses_on_main(git_repo):
    (git_repo / "new.py").write_text("x = 1")
    with pytest.raises(DevopsError, match="main"):
        commit_ticket(git_repo, _ticket())


def test_create_branch_without_slug(git_repo):
    branch = create_branch(git_repo, "add-subtract", "")
    assert branch == "issue-add-subtract"


def test_commit_ticket_skips_default_junk(git_repo):
    from issue_runner.phases.devops import DEFAULT_EXCLUDES, ensure_excluded

    create_branch(git_repo, "17", "x")
    for pattern in DEFAULT_EXCLUDES:
        ensure_excluded(git_repo, pattern)
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "junk.pyc").write_text("x")
    (git_repo / "real.py").write_text("x = 1")
    commit_ticket(git_repo, _ticket())
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=git_repo, capture_output=True, text=True, check=False
    ).stdout
    assert "real.py" in tracked
    assert "junk.pyc" not in tracked
