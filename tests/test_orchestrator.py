import json
import subprocess

import pytest

from issue_runner.orchestrator import run_issue
from issue_runner.tickets import TicketStore
from tests.conftest import FakeClient

ISSUE = {"number": 17, "title": "Add subtract", "body": "need it", "url": ""}


def plan_reply():
    return json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {
                    "title": "subtract ints",
                    "description": "implement subtract",
                    "test_assertion": "subtract(5,3) == 2",
                }
            ],
        }
    )


def verdict(v, tf="make it stronger", cf="fix the code"):
    return json.dumps({"verdict": v, "reasons": ["r"], "test_feedback": tf, "code_feedback": cf})


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


def write_test(repo, content):
    return lambda: (repo / "test_sub.py").write_text(content)


def implement(repo):
    return lambda: (repo / "impl.py").write_text("code")


def test_happy_path_single_ticket(git_repo, cfg):
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1 and report.blocked == 0
    log = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=git_repo, capture_output=True, text=True, check=False
    ).stdout
    assert "subtract ints" in log
    roles = [c["role"] for c in client.calls]
    assert roles == ["planner", "builder.tester", "builder.coder", "verifier"]


def test_rework_code_loops_back_to_coder(git_repo, cfg):
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("rework_code"), None),
            ("reworked", None),  # coder again; still green via impl.py
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1
    roles = [c["role"] for c in client.calls]
    assert roles == [
        "planner",
        "builder.tester",
        "builder.coder",
        "verifier",
        "builder.coder",
        "verifier",
    ]
    # the verifier's code feedback must reach the coder
    assert "fix the code" in client.calls[4]["prompt"]


def test_refine_test_loops_back_to_tester(git_repo, cfg):
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("refine_test"), None),
            # refined test is green immediately (impl already exists) -> no coder call
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED # more")),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1
    roles = [c["role"] for c in client.calls]
    assert roles == [
        "planner",
        "builder.tester",
        "builder.coder",
        "verifier",
        "builder.tester",
        "verifier",
    ]
    assert "make it stronger" in client.calls[4]["prompt"]


def test_max_rounds_blocks_ticket(git_repo, cfg):
    # cfg.max_rounds == 2 -> initial verify + 2 hand-backs, then blocked
    client = FakeClient(
        [
            (plan_reply(), None),
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
    assert report.done == 0 and report.blocked == 1


def test_resume_skips_planning(git_repo, cfg):
    store = TicketStore(git_repo / ".state", issue_ref="17")
    from issue_runner.tickets import Ticket

    done = Ticket(id=1, title="t", description="d", test_assertion="a")
    done.status = "done"
    store.set_tickets([done])
    store.save()

    client = FakeClient([])  # no calls expected: plan skipped, no pending tickets
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1
    assert client.calls == []


def test_copilot_error_blocks_ticket_not_run(git_repo, cfg):
    from issue_runner.copilot import CopilotError

    class ExplodingClient(FakeClient):
        def run(self, prompt, role, read_only=False, session_name=None):
            if role == "builder.tester":
                raise CopilotError("copilot timed out")
            return super().run(prompt, role, read_only, session_name)

    client = ExplodingClient([(plan_reply(), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.blocked == 1 and report.done == 0


class RecordingBackend:
    def __init__(self):
        self.created = []
        self.closed = []
        self._next = 100

    def create(self, parent_number, ticket):
        self.created.append((parent_number, ticket.id))
        self._next += 1
        return self._next

    def close(self, number, comment):
        self.closed.append((number, comment))


def test_backend_mirrors_tickets_and_closes_on_pass(git_repo, cfg):
    backend = RecordingBackend()
    cfg.tickets_backend = backend
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1
    assert backend.created == [(17, 1)]
    assert len(backend.closed) == 1
    assert backend.closed[0][0] == 101


def test_backend_backfills_on_resume(git_repo, cfg):
    from issue_runner.tickets import Ticket

    store = TicketStore(git_repo / ".state", issue_ref="17")
    mirrored = Ticket(id=1, title="a", description="d", test_assertion="x")
    mirrored.status = "done"
    mirrored.github_issue = 55
    unmirrored = Ticket(id=2, title="b", description="d", test_assertion="y")
    unmirrored.status = "done"
    store.set_tickets([mirrored, unmirrored])
    store.save()

    backend = RecordingBackend()
    cfg.tickets_backend = backend
    report = run_issue(cfg, FakeClient([]), ISSUE, state_dir=git_repo / ".state")
    assert report.done == 2
    # only the unmirrored ticket gets a new Gitea issue
    assert backend.created == [(17, 2)]
    reloaded = TicketStore(git_repo / ".state", issue_ref="17")
    reloaded.load()
    assert reloaded.tickets[1].github_issue == 101
    # a backfilled ticket that is already done must not be left open in the tracker
    assert backend.closed == [(101, "completed in an earlier run")]
