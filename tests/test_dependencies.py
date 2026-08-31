import json
import subprocess

import pytest

from issue_runner.orchestrator import run_issue
from issue_runner.tickets import Ticket, TicketStore
from tests.conftest import FakeClient

ISSUE = {"number": 17, "title": "Add subtract", "body": "need it", "url": ""}


def make_store(tmp_path, specs):
    """specs: list of (id, status, depends_on)."""
    store = TicketStore(tmp_path / ".state", issue_ref="17")
    tickets = []
    for tid, status, deps in specs:
        ticket = Ticket(
            id=tid, title=f"t{tid}", description="d", test_assertion="a", depends_on=list(deps)
        )
        ticket.status = status
        tickets.append(ticket)
    store.set_tickets(tickets)
    return store


def ids(tickets):
    return [t.id for t in tickets]


def test_ready_excludes_tickets_with_unmet_dependencies(tmp_path):
    store = make_store(tmp_path, [(1, "pending", []), (2, "pending", [1])])
    assert ids(store.ready()) == [1]


def test_ready_includes_a_ticket_once_its_dependency_is_done(tmp_path):
    store = make_store(tmp_path, [(1, "done", []), (2, "pending", [1])])
    assert ids(store.ready()) == [2]


def test_ready_requires_every_dependency(tmp_path):
    store = make_store(tmp_path, [(1, "done", []), (2, "pending", []), (3, "pending", [1, 2])])
    assert ids(store.ready()) == [2]


def test_ready_preserves_plan_order(tmp_path):
    store = make_store(tmp_path, [(1, "pending", []), (2, "pending", []), (3, "pending", [])])
    assert ids(store.ready()) == [1, 2, 3]


def test_ready_excludes_done_and_blocked(tmp_path):
    store = make_store(tmp_path, [(1, "done", []), (2, "blocked", []), (3, "pending", [])])
    assert ids(store.ready()) == [3]


def test_ready_excludes_a_dependent_of_a_blocked_ticket(tmp_path):
    store = make_store(tmp_path, [(1, "blocked", []), (2, "pending", [1])])
    assert store.ready() == []


def test_ready_is_empty_for_a_dependency_cycle(tmp_path):
    store = make_store(tmp_path, [(1, "pending", [2]), (2, "pending", [1])])
    assert store.ready() == []


def test_ready_excludes_a_ticket_depending_on_itself(tmp_path):
    store = make_store(tmp_path, [(1, "pending", [1])])
    assert store.ready() == []


def test_ready_excludes_a_ticket_with_an_unknown_dependency(tmp_path):
    store = make_store(tmp_path, [(1, "pending", [99])])
    assert store.ready() == []


def test_blocked_dependency_reason_names_the_culprit(tmp_path):
    store = make_store(tmp_path, [(1, "blocked", []), (2, "pending", [1])])
    reason = store.unsatisfiable_reason(store.tickets[1])
    assert "1" in reason and "blocked" in reason


def test_unknown_dependency_reason_is_explicit(tmp_path):
    store = make_store(tmp_path, [(1, "pending", [99])])
    assert "unknown" in store.unsatisfiable_reason(store.tickets[0])


def test_cycle_reason_is_explicit(tmp_path):
    store = make_store(tmp_path, [(1, "pending", [2]), (2, "pending", [1])])
    assert "cycle" in store.unsatisfiable_reason(store.tickets[0])


# --- end-to-end through the orchestrator -------------------------------------


@pytest.fixture
def git_repo(repo):
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "runner@test.local")
    git("config", "user.name", "Runner Test")
    git("add", "-A")
    git("commit", "-m", "seed")
    return repo


def plan_with_deps():
    """Ticket 1 depends on ticket 2, so the plan order is NOT the run order."""
    return json.dumps(
        {
            "summary": "two tickets, reversed",
            "tickets": [
                {
                    "title": "dependent",
                    "description": "d",
                    "test_assertion": "a == 1",
                    "depends_on": [2],
                },
                {"title": "prerequisite", "description": "d", "test_assertion": "b == 2"},
            ],
        }
    )


def write_test(repo, content):
    return lambda: (repo / "test_sub.py").write_text(content)


def implement(repo):
    return lambda: (repo / "impl.py").write_text("code")


def verdict(v):
    return json.dumps({"verdict": v, "reasons": ["r"], "test_feedback": "t", "code_feedback": "c"})


def test_dependent_ticket_runs_after_its_prerequisite(git_repo, cfg):
    def second_test():
        # a fresh red: drop the previous impl so the new test genuinely fails
        (git_repo / "impl.py").unlink(missing_ok=True)
        (git_repo / "test_sub2.py").write_text("assert RED")

    client = FakeClient(
        [
            (plan_with_deps(), None),
            # prerequisite (ticket 2) must be built FIRST despite being second
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
            (json.dumps({"test_path": "test_sub2.py"}), second_test),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 2 and report.blocked == 0
    order = [c["prompt"] for c in client.calls if c["role"] == "builder.tester"]
    assert "prerequisite" in order[0]
    assert "dependent" in order[1]


def test_ticket_with_unmet_dependency_never_reaches_the_tester(git_repo, cfg):
    """The prerequisite blocks, so the dependent must never be built."""
    client = FakeClient(
        [
            (plan_with_deps(), None),
            # ticket 2 (prerequisite) runs first and never converges -> blocked
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("rework_code"), None),
            ("rework 1", None),
            (verdict("rework_code"), None),
            ("rework 2", None),
            (verdict("rework_code"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 0
    assert report.blocked == 2, "the dependent must be blocked, not silently skipped"
    testers = [c["prompt"] for c in client.calls if c["role"] == "builder.tester"]
    assert len(testers) == 1 and "prerequisite" in testers[0]
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    dependent = next(t for t in store.tickets if t.title == "dependent")
    assert dependent.status == "blocked"
    assert "2" in dependent.blocked_reason and "blocked" in dependent.blocked_reason


def test_dependency_cycle_blocks_both_tickets_without_model_calls(git_repo, cfg):
    plan = json.dumps(
        {
            "summary": "cycle",
            "tickets": [
                {"title": "a", "description": "d", "test_assertion": "x", "depends_on": [2]},
                {"title": "b", "description": "d", "test_assertion": "y", "depends_on": [1]},
            ],
        }
    )
    client = FakeClient([(plan, None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.blocked == 2 and report.done == 0
    assert [c["role"] for c in client.calls] == ["planner"]
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert all("cycle" in t.blocked_reason for t in store.tickets)


def test_budget_stop_leaves_dependents_pending_not_blocked(git_repo, cfg):
    from issue_runner.budget import BudgetExhausted

    class BrokeClient(FakeClient):
        def run(self, prompt, role, read_only=False, session_name=None):
            if role == "builder.tester":
                raise BudgetExhausted("run credit budget exhausted")
            return super().run(prompt, role, read_only, session_name)

    client = BrokeClient([(plan_with_deps(), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.budget_exhausted is True
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    dependent = next(t for t in store.tickets if t.title == "dependent")
    assert dependent.status == "pending", "a budget stop must stay resumable"
