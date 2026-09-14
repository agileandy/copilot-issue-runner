from pathlib import Path

import pytest

from issue_runner.orchestrator import run_issue
from issue_runner.phases import devops
from issue_runner.phases.devops import DevopsError
from issue_runner.tickets import Ticket
from tests.hardening_support import ISSUE, DemoClient, git, load_store, sandbox


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_source_is_refused_before_any_model_call(tmp_path, staged):
    env, cfg = sandbox(tmp_path, isolated=True)
    note = env.repo_dir / "user-note.txt"
    note.write_text("do not commit this\n")
    if staged:
        git(env.repo_dir, "add", "user-note.txt")
    before = git(env.repo_dir, "rev-parse", "HEAD")
    client = DemoClient(cfg)

    with pytest.raises(DevopsError, match="dirty"):
        run_issue(cfg, client, ISSUE)
    assert client.roles == []
    assert note.read_text() == "do not commit this\n"
    assert git(env.repo_dir, "rev-parse", "HEAD") == before
    assert git(env.repo_dir, "branch", "--show-current") == "main"


def test_run_is_isolated_and_source_checkout_stays_unchanged(tmp_path):
    env, cfg = sandbox(tmp_path, isolated=True)
    source_head = git(env.repo_dir, "rev-parse", "HEAD")
    source_code = (env.repo_dir / "demo_pkg" / "stats.py").read_bytes()
    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    workspace = Path(report.worktree)

    assert (report.done, report.blocked) == (1, 0)
    assert workspace != env.repo_dir
    assert git(env.repo_dir, "branch", "--show-current") == "main"
    assert git(env.repo_dir, "rev-parse", "HEAD") == source_head
    assert (env.repo_dir / "demo_pkg" / "stats.py").read_bytes() == source_code
    assert git(env.repo_dir, "status", "--porcelain") == ""
    assert git(workspace, "status", "--porcelain") == ""
    assert ".issue-runner" not in git(workspace, "show", "--pretty=", "--name-only", "HEAD")
    assert load_store(env).worktree == str(workspace)


def test_failed_ticket_work_is_retained_and_no_later_ticket_runs(tmp_path):
    env, cfg = sandbox(tmp_path, coder_retries=0, max_rounds=0)

    class BadCoder(DemoClient):
        def run(self, prompt, role, **kwargs):
            if role == "builder.coder":
                self.roles.append(role)
                (Path(cfg.repo_dir) / "blocked-residue.txt").write_text("failed attempt\n")
                return '{"changed_files":["blocked-residue.txt"]}'
            return super().run(prompt, role, **kwargs)

    client = BadCoder(cfg, one_ticket=False)
    report = run_issue(cfg, client, ISSUE)
    store = load_store(env)
    assert (report.done, report.blocked) == (0, 1)
    assert [t.status for t in store.tickets] == ["blocked", "pending"]
    assert client.roles == ["planner", "builder.tester", "builder.coder"]
    assert "blocked-residue.txt" not in git(env.repo_dir, "ls-files")
    assert (env.repo_dir / "blocked-residue.txt").read_text() == "failed attempt\n"

    resumed = DemoClient(cfg)
    again = run_issue(cfg, resumed, ISSUE)
    assert again.blocked == 1 and resumed.roles == []
    assert "--retry-blocked" in " ".join(again.details)


def test_a_new_file_after_approval_is_not_staged(tmp_path):
    env, _ = sandbox(tmp_path)
    for pattern in devops.DEFAULT_EXCLUDES:
        devops.ensure_excluded(env.repo_dir, pattern)
    branch = devops.create_branch(env.repo_dir, "17", "scope")
    ticket = Ticket(id=1, title="change", description="d", test_assertion="a")
    ticket.base_commit = devops.head_commit(env.repo_dir)
    (env.repo_dir / "approved.py").write_text("VALUE = 1\n")
    devops.approve_changes(env.repo_dir, ticket)
    (env.repo_dir / "user.py").write_text("UNRELATED = 2\n")
    with pytest.raises(DevopsError, match="changed after approval"):
        devops.commit_ticket(env.repo_dir, ticket, expected_branch=branch)
    assert git(env.repo_dir, "diff", "--cached", "--name-only") == ""
    assert (env.repo_dir / "user.py").read_text() == "UNRELATED = 2\n"


def test_second_runner_cannot_enter_an_owned_repository(tmp_path):
    env, cfg = sandbox(tmp_path)
    client = DemoClient(cfg)
    with (
        devops.repository_lock(env.repo_dir),
        pytest.raises(DevopsError, match="another issue-runner"),
    ):
        run_issue(cfg, client, ISSUE)
    assert client.roles == []


def test_linked_worktree_exclusions_use_the_real_git_directory(tmp_path):
    env, _ = sandbox(tmp_path)
    other = tmp_path / "linked"
    git(env.repo_dir, "worktree", "add", "-b", "issue-17-linked", str(other))
    assert (other / ".git").is_file()
    devops.ensure_excluded(other, ".issue-runner/")
    state = other / ".issue-runner"
    state.mkdir()
    (state / "private.json").write_text("{}")
    assert git(other, "status", "--porcelain") == ""
