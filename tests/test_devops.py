import subprocess

import pytest

from issue_runner.phases.devops import (
    DevopsError,
    approve_changes,
    branch_kind,
    branch_name,
    commit_ticket,
    create_branch,
    current_branch,
    is_run_branch,
)
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
    branch = create_branch(git_repo, "feature/17-add-subtract")
    assert branch == "feature/17-add-subtract"
    assert current_branch(git_repo) == "feature/17-add-subtract"


def test_create_branch_reuses_existing(git_repo):
    create_branch(git_repo, "feature/17-add-subtract")
    subprocess.run(["git", "switch", "main"], cwd=git_repo, check=True, capture_output=True)
    branch = create_branch(git_repo, "feature/17-add-subtract")
    assert branch == "feature/17-add-subtract"
    assert current_branch(git_repo) == "feature/17-add-subtract"


def test_commit_ticket_commits_changes(git_repo):
    create_branch(git_repo, "feature/17-add-subtract")
    (git_repo / "new.py").write_text("x = 1")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    sha = commit_ticket(git_repo, ticket)
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


def test_branch_name_without_slug():
    assert branch_name("add-subtract", "") == "feature/add-subtract"


def test_branch_names_follow_the_team_prefixes():
    assert branch_name("17", "add-subtract") == "feature/17-add-subtract"
    assert branch_name("17", "fix-crash", "bugfix") == "bugfix/17-fix-crash"
    with pytest.raises(DevopsError):
        branch_name("17", "x", "issue")


@pytest.mark.parametrize(
    ("labels", "kind"),
    [
        ([], "feature"),
        ([{"name": "enhancement"}], "feature"),
        ([{"name": "Bug"}], "bugfix"),
        (["bug"], "bugfix"),
        ([{"name": "bugfix-later"}], "feature"),
    ],
)
def test_bug_label_selects_a_bugfix_branch(labels, kind):
    assert branch_kind({"number": 1, "title": "t", "labels": labels}) == kind


def test_issue_without_labels_is_a_feature():
    assert branch_kind({"number": 1, "title": "t"}) == "feature"


@pytest.mark.parametrize("branch", ["feature/17-x", "bugfix/17-x", "hotfix/17-x", "issue-17-x"])
def test_run_branches_include_legacy_issue_branches(branch):
    assert is_run_branch(branch)


@pytest.mark.parametrize("branch", ["main", "master", "release/1", "issues"])
def test_other_branches_are_not_run_branches(branch):
    assert not is_run_branch(branch)


def test_legacy_issue_branch_still_commits(git_repo):
    create_branch(git_repo, "issue-17-legacy")
    (git_repo / "new.py").write_text("x = 1")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    assert commit_ticket(git_repo, ticket, expected_branch="issue-17-legacy")


def test_commit_ticket_skips_default_junk(git_repo):
    from issue_runner.phases.devops import DEFAULT_EXCLUDES, ensure_excluded

    create_branch(git_repo, "feature/17-x")
    for pattern in DEFAULT_EXCLUDES:
        ensure_excluded(git_repo, pattern)
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "junk.pyc").write_text("x")
    (git_repo / "real.py").write_text("x = 1")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    commit_ticket(git_repo, ticket)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=git_repo, capture_output=True, text=True, check=False
    ).stdout
    assert "real.py" in tracked
    assert "junk.pyc" not in tracked


def test_commit_ticket_requires_explicit_approval(git_repo):
    create_branch(git_repo, "feature/17-approval")
    (git_repo / "new.py").write_text("x = 1")
    with pytest.raises(DevopsError, match="no approved change set"):
        commit_ticket(git_repo, _ticket())


def test_worktree_state_reports_untracked_file(git_repo):
    from issue_runner.phases.devops import head_commit, worktree_state

    (git_repo / "notes.md").write_text("notes")
    assert worktree_state(git_repo) == {
        "path": str(git_repo),
        "branch": "main",
        "head": head_commit(git_repo),
        "dirty": True,
        "uncommitted": ["notes.md"],
    }


def test_untracked_uv_lock_stays_out_of_the_ticket_commit(git_repo):
    from issue_runner.phases.devops import files_in_commit

    create_branch(git_repo, "feature/151-x")
    (git_repo / "real.py").write_text("x = 1")
    (git_repo / "uv.lock").write_text("version = 1\n")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    sha = commit_ticket(git_repo, ticket)
    assert files_in_commit(git_repo, sha) == ["real.py"]


def test_untracked_uv_lock_stays_out_of_the_review_fix_commit(git_repo):
    from issue_runner.phases.devops import commit_changes, files_in_commit

    create_branch(git_repo, "feature/151-x")
    (git_repo / "seed.txt").write_text("seed changed")
    (git_repo / "uv.lock").write_text("version = 1\n")
    sha = commit_changes(git_repo, "fix(review): r1", "feature/151-x")
    assert files_in_commit(git_repo, sha) == ["seed.txt"]


def test_untracked_uv_lock_stays_out_of_the_branch_update_merge_commit(git_repo):
    from issue_runner.phases.devops import files_in_commit, finish_merge, merge_in

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    create_branch(git_repo, "feature/151-x")
    git("checkout", "main")
    (git_repo / "other.txt").write_text("other")
    git("add", "other.txt")
    git("commit", "-m", "other")
    git("checkout", "feature/151-x")
    (git_repo / "uv.lock").write_text("version = 1\n")
    merge_in(git_repo, "main", "merge main")
    sha = finish_merge(git_repo, "feature/151-x")
    assert files_in_commit(git_repo, sha) == ["other.txt"]


def test_uv_lock_tracked_by_merged_ref_stays_in_the_branch_update_merge_commit(git_repo):
    from issue_runner.phases.devops import files_in_commit, finish_merge, merge_in

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    create_branch(git_repo, "feature/151-x")
    git("checkout", "main")
    (git_repo / "uv.lock").write_text("version = 1\n")
    (git_repo / "feature.txt").write_text("feature")
    git("add", "uv.lock", "feature.txt")
    git("commit", "-m", "add uv.lock and feature")
    git("checkout", "feature/151-x")
    merge_in(git_repo, "main", "merge main")
    sha = finish_merge(git_repo, "feature/151-x")
    assert files_in_commit(git_repo, sha) == ["feature.txt", "uv.lock"]


def test_shared_changes_reports_only_committed_source_with_other_tests(git_repo):
    from issue_runner.phases.devops import shared_changes

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    (git_repo / "tests").mkdir()
    (git_repo / "feature.py").write_text("def feature():\n    return 1\n")
    (git_repo / "tests" / "test_existing.py").write_text(
        "import feature\n\n\ndef test_feature():\n    assert feature.feature() == 1\n"
    )
    git("add", "feature.py", "tests/test_existing.py")
    git("commit", "-m", "feature")
    (git_repo / "added.py").write_text("def added():\n    return 2\n")
    (git_repo / "tests" / "test_added.py").write_text(
        "import added\nimport feature\n\n\ndef test_added():\n    assert added.added() == 2\n"
    )

    shared = shared_changes(
        git_repo, ["added.py", "feature.py", "tests/test_added.py"], "tests/test_added.py"
    )

    assert shared == ["feature.py"]


def test_shared_changes_treats_existing_non_python_source_as_shared(git_repo):
    from issue_runner.phases.devops import shared_changes

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    (git_repo / "tests").mkdir()
    (git_repo / "app.js").write_text("module.exports = 1;\n")
    git("add", "app.js")
    git("commit", "-m", "app")
    (git_repo / "app.js").write_text("module.exports = 2;\n")
    (git_repo / "tests" / "test_added.py").write_text("def test_added():\n    assert 1 + 1 == 2\n")

    shared = shared_changes(git_repo, ["app.js", "tests/test_added.py"], "tests/test_added.py")

    assert shared == ["app.js"]


def test_branch_lock_is_shared_by_every_worktree_of_a_repository(tmp_path):
    from contextlib import ExitStack

    from issue_runner.phases import devops
    from tests.hardening_support import git, sandbox

    env, _ = sandbox(tmp_path)
    linked = tmp_path / "linked"
    git(env.repo_dir, "worktree", "add", "-b", "wt-linked", str(linked))
    branch = "feature/17-add-statistics"
    with devops.branch_lock(env.repo_dir, branch), ExitStack() as stack:
        with pytest.raises(DevopsError, match=f"another issue-runner owns this branch {branch}"):
            stack.enter_context(devops.branch_lock(linked, branch))


def test_ensure_excluded_keeps_every_pattern_when_worktrees_race(tmp_path, monkeypatch):
    import pathlib
    import threading

    from issue_runner.phases import devops
    from tests.hardening_support import git, sandbox

    env, _ = sandbox(tmp_path)
    worktree_a = tmp_path / "a"
    worktree_b = tmp_path / "b"
    git(env.repo_dir, "worktree", "add", "-b", "wt-a", str(worktree_a))
    git(env.repo_dir, "worktree", "add", "-b", "wt-b", str(worktree_b))
    exclude = devops.git_common_dir(env.repo_dir) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if not exclude.exists():
        exclude.write_text("")

    barrier = threading.Barrier(2, timeout=1)
    original_read_text = pathlib.Path.read_text

    def racing_read_text(self, *args, **kwargs):
        existing = original_read_text(self, *args, **kwargs)
        if self.name == "exclude":
            # Wait after reading: without the lock both threads then write from the same snapshot.
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # With the lock the other thread is kept out, so the wait times out.
                pass
        return existing

    monkeypatch.setattr(pathlib.Path, "read_text", racing_read_text)
    threads = [
        threading.Thread(target=devops.ensure_excluded, args=(worktree_a, "/.issue-runner-a/")),
        threading.Thread(target=devops.ensure_excluded, args=(worktree_b, "/.issue-runner-b/")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.undo()

    lines = exclude.read_text().splitlines()
    assert {"/.issue-runner-a/", "/.issue-runner-b/"} <= set(lines)


def test_commit_ticket_commits_an_already_staged_deletion(git_repo):
    from issue_runner.phases.devops import files_in_commit

    create_branch(git_repo, "feature/150-x")
    subprocess.run(["git", "rm", "-q", "seed.txt"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "new.py").write_text("x = 1")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    sha = commit_ticket(git_repo, ticket)
    assert files_in_commit(git_repo, sha) == ["new.py", "seed.txt"]


def test_commit_ticket_commits_an_unstaged_deletion(git_repo):
    from issue_runner.phases.devops import files_in_commit

    create_branch(git_repo, "feature/150-x")
    (git_repo / "seed.txt").unlink()
    (git_repo / "new.py").write_text("x = 1")
    ticket = _ticket()
    approve_changes(git_repo, ticket)
    sha = commit_ticket(git_repo, ticket)
    assert files_in_commit(git_repo, sha) == ["new.py", "seed.txt"]


def test_commit_changes_commits_an_already_staged_deletion(git_repo):
    from issue_runner.phases.devops import commit_changes, files_in_commit

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    (git_repo / "uv.lock").write_text("version = 1\n")
    git("add", "uv.lock")
    git("commit", "-m", "add uv.lock")
    create_branch(git_repo, "feature/150-x")
    git("rm", "-q", "uv.lock")
    (git_repo / "seed.txt").write_text("seed changed")
    sha = commit_changes(git_repo, "fix(review): r1", "feature/150-x")
    assert files_in_commit(git_repo, sha) == ["seed.txt", "uv.lock"]


def test_commit_changes_commits_an_unstaged_deletion(git_repo):
    from issue_runner.phases.devops import commit_changes, files_in_commit

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    (git_repo / "uv.lock").write_text("version = 1\n")
    git("add", "uv.lock")
    git("commit", "-m", "add uv.lock")
    create_branch(git_repo, "feature/150-x")
    (git_repo / "uv.lock").unlink()
    (git_repo / "seed.txt").write_text("seed changed")
    sha = commit_changes(git_repo, "fix(review): r1", "feature/150-x")
    assert files_in_commit(git_repo, sha) == ["seed.txt", "uv.lock"]


def test_finish_merge_commits_a_deletion_from_the_merged_ref(git_repo):
    from issue_runner.phases.devops import files_in_commit, finish_merge, merge_in

    def git(*args):
        subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True)

    create_branch(git_repo, "feature/150-x")
    git("checkout", "main")
    git("rm", "-q", "seed.txt")
    git("commit", "-m", "remove seed")
    git("checkout", "feature/150-x")
    merge_in(git_repo, "main", "merge main")
    sha = finish_merge(git_repo, "feature/150-x")
    assert files_in_commit(git_repo, sha) == ["seed.txt"]
