from pathlib import Path

import pytest

from issue_runner import orchestrator, storage
from issue_runner.budget import BudgetExhausted
from issue_runner.orchestrator import run_issue
from issue_runner.phases import devops
from issue_runner.tickets import StateError, Ticket, TicketStore
from tests.hardening_support import ISSUE, DemoClient, Interrupted, git, load_store, sandbox


@pytest.mark.parametrize(
    ("before", "saved_phase"),
    [("builder.coder", "coder"), ("verifier", "verifier")],
)
def test_resume_starts_with_the_first_unaccepted_phase(tmp_path, before, saved_phase):
    env, cfg = sandbox(tmp_path, isolated=True)
    with pytest.raises(Interrupted):
        run_issue(cfg, DemoClient(cfg, before=before), ISSUE)
    store = load_store(env)
    ticket = store.tickets[0]
    assert ticket.phase == saved_phase
    assert ticket.test_snapshot and ticket.test_hash
    assert ticket.base_commit and ticket.commit_token

    resumed = DemoClient(cfg)
    report = run_issue(cfg, resumed, ISSUE)
    assert (report.done, report.blocked) == (1, 0)
    assert resumed.roles[0] == before
    assert "planner" not in resumed.roles and "builder.tester" not in resumed.roles
    assert load_store(env).tickets[0].commit_sha == git(Path(report.worktree), "rev-parse", "HEAD")


def test_resume_after_verdict_does_not_repeat_verification(tmp_path, monkeypatch):
    env, cfg = sandbox(tmp_path)
    original = orchestrator._regression_gate

    def interrupt_gate(cfg):
        raise Interrupted("before regression")

    with monkeypatch.context() as scoped:
        scoped.setattr(orchestrator, "_regression_gate", interrupt_gate)
        with pytest.raises(Interrupted):
            run_issue(cfg, DemoClient(cfg), ISSUE)
    assert load_store(env).tickets[0].phase == "regression"
    assert orchestrator._regression_gate is original
    resumed = DemoClient(cfg)
    assert run_issue(cfg, resumed, ISSUE).done == 1
    assert resumed.roles == []


def test_commit_is_recovered_after_git_succeeds_before_state_save(tmp_path, monkeypatch):
    env, cfg = sandbox(tmp_path)
    original = devops.commit_ticket

    def interrupt_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise Interrupted("after git commit")

    with monkeypatch.context() as scoped:
        scoped.setattr(devops, "commit_ticket", interrupt_after_commit)
        with pytest.raises(Interrupted):
            run_issue(cfg, DemoClient(cfg), ISSUE)
    store = load_store(env)
    assert store.tickets[0].phase == "commit"
    assert store.tickets[0].commit_sha is None
    committed = git(env.repo_dir, "rev-parse", "HEAD")
    resumed = DemoClient(cfg)
    assert run_issue(cfg, resumed, ISSUE).done == 1
    assert resumed.roles == []
    assert git(env.repo_dir, "rev-parse", "HEAD") == committed
    assert load_store(env).tickets[0].commit_sha == committed
    assert git(env.repo_dir, "rev-list", "--count", "HEAD") == "2"


def test_commit_resumes_after_staging_without_repeating_model_calls(tmp_path, monkeypatch):
    env, cfg = sandbox(tmp_path)
    original = devops._git

    def interrupt_before_commit(repo_dir, *args, **kwargs):
        if args[0] == "commit":
            raise Interrupted("before git commit")
        return original(repo_dir, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(devops, "_git", interrupt_before_commit)
        with pytest.raises(Interrupted):
            run_issue(cfg, DemoClient(cfg), ISSUE)
    assert git(env.repo_dir, "diff", "--cached", "--name-only")
    resumed = DemoClient(cfg)
    assert run_issue(cfg, resumed, ISSUE).done == 1
    assert resumed.roles == []
    assert git(env.repo_dir, "status", "--porcelain") == ""


def test_atomic_save_preserves_the_previous_state_on_replace_failure(tmp_path, monkeypatch):
    store = TicketStore(tmp_path, "17")
    store.set_tickets([Ticket(id=1, title="t", description="d", test_assertion="a")])
    store.save()
    previous = store.state_file.read_bytes()
    store.tickets[0].phase = "coder"

    def fail_replace(*args):
        raise OSError("interrupted replacement")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(StateError, match="could not save"):
        store.save()
    assert store.state_file.read_bytes() == previous
    assert list(tmp_path.glob(".issue-17.json.*")) == []


def test_corrupt_state_is_reported_without_overwriting_it(tmp_path):
    path = tmp_path / "issue-17.json"
    path.write_text('{"tickets":')
    with pytest.raises(StateError, match="cannot resume"):
        TicketStore(tmp_path, "17").load()
    assert path.read_text() == '{"tickets":'


def test_changed_accepted_test_is_preserved_and_not_committed(tmp_path):
    env, cfg = sandbox(tmp_path)
    with pytest.raises(Interrupted):
        run_issue(cfg, DemoClient(cfg, before="verifier"), ISSUE)
    path = env.repo_dir / load_store(env).tickets[0].test_path
    path.write_text("def test_changed():\n    assert True\n")
    resumed = DemoClient(cfg)
    report = run_issue(cfg, resumed, ISSUE)
    assert (report.done, report.blocked) == (0, 1)
    assert resumed.roles == []
    assert path.read_text() == "def test_changed():\n    assert True\n"
    assert git(env.repo_dir, "rev-list", "--count", "HEAD") == "1"


def test_budget_pause_resumes_without_retry_blocked_or_repeating_the_tester(tmp_path):
    env, cfg = sandbox(tmp_path)

    class BudgetStop(DemoClient):
        def run(self, prompt, role, **kwargs):
            if role == "builder.coder":
                raise BudgetExhausted("stop before coder")
            return super().run(prompt, role, **kwargs)

    report = run_issue(cfg, BudgetStop(cfg), ISSUE)
    ticket = load_store(env).tickets[0]
    assert report.budget_exhausted and report.blocked == 0
    assert (ticket.status, ticket.phase) == ("pending", "coder")
    resumed = DemoClient(cfg)
    assert run_issue(cfg, resumed, ISSUE).done == 1
    assert resumed.roles == ["builder.coder", "verifier"]


def test_resume_uses_saved_branch_despite_a_changed_issue_title(tmp_path):
    env, cfg = sandbox(tmp_path, isolated=True)
    with pytest.raises(Interrupted):
        run_issue(cfg, DemoClient(cfg, before="verifier"), ISSUE)
    branch = load_store(env).branch
    changed = {**ISSUE, "title": "A renamed issue"}
    report = run_issue(cfg, DemoClient(cfg), changed)
    assert report.done == 1 and report.branch == branch
