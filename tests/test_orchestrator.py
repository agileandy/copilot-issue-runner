import json
import subprocess
import sys

import pytest

from issue_runner.orchestrator import run_issue
from issue_runner.tickets import TicketStore
from issue_runner.visual import render_flow
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


def test_render_flow_snapshot_contract():
    assert (
        render_flow(
            {"plan": "done", "branch": "active", "tickets": [{"id": 1, "status": "pending"}]}
        )
        == "issue pipeline\nplan: done\n   ↓\nbranch: active\n   ↓\nper-ticket build/verify:\n  ticket #1: pending\n   ↓\ncommit: pending"
        and render_flow({"plan": "done", "branch": "active", "tickets": []})
        == "issue pipeline\nplan: done\n   ↓\nbranch: active\n   ↓\nper-ticket build/verify:\n  tickets: none\n   ↓\ncommit: pending"
    )


def test_render_flow_includes_ticket_id_in_snapshot():
    assert "ticket 1" in render_flow(
        {
            "plan": "done",
            "branch": "done",
            "tickets": [{"id": 1, "title": "subtract ints", "status": "in_progress"}],
        }
    )


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
    first = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    assert run_issue(cfg, first, ISSUE, state_dir=git_repo / ".state").done == 1

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
        self.blocked = []
        self._next = 100

    def create(self, parent_number, ticket):
        self.created.append((parent_number, ticket.id))
        self._next += 1
        return self._next

    def close(self, ticket, comment):
        self.closed.append((ticket.id, comment))

    def block(self, ticket, reason):
        self.blocked.append((ticket.id, reason))


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
    assert backend.closed[0][0] == 1
    assert "Done in " in backend.closed[0][1]


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
    report = run_issue(cfg, FakeClient([]), ISSUE, state_dir=git_repo / ".state", plan_only=True)
    assert report.done == 2
    # only the unmirrored ticket gets a new Gitea issue
    assert backend.created == [(17, 2)]
    reloaded = TicketStore(git_repo / ".state", issue_ref="17")
    reloaded.load()
    assert reloaded.tickets[1].github_issue == 101
    # a backfilled ticket that is already done must not be left open in the tracker
    assert backend.closed == [(2, "completed in an earlier run")]


def already_green_script(git_repo):
    """Tester writes a green (already-satisfied) test on every attempt."""
    return [
        (plan_reply(), None),
        (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert PASS # v1")),
        (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert PASS # v2")),
    ]


def test_already_satisfied_ticket_arbitrated_done(git_repo, cfg):
    backend = RecordingBackend()
    cfg.tickets_backend = backend
    client = FakeClient([*already_green_script(git_repo), (verdict("pass"), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1 and report.blocked == 0
    roles = [c["role"] for c in client.calls]
    assert roles == ["planner", "builder.tester", "builder.tester", "verifier"]
    # the regression test the tester wrote gets committed
    log = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=git_repo, capture_output=True, text=True, check=False
    ).stdout
    assert "subtract ints" in log
    assert "already satisfied" in backend.closed[0][1]


def test_already_satisfied_hand_back_respects_round_limit(git_repo, cfg):
    backend = RecordingBackend()
    cfg.tickets_backend = backend
    cfg.max_rounds = 0
    client = FakeClient(
        [*already_green_script(git_repo), (verdict("refine_test", tf="test is a tautology"), None)]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.blocked == 1
    assert backend.blocked, "backend.block must be called with the reason"
    assert backend.blocked[0][0] == 1
    assert "max_rounds=0" in backend.blocked[0][1]
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert store.tickets[0].test_feedback == "test is a tautology"


def test_initially_green_test_can_be_refined_to_expose_and_fix_a_real_bug(git_repo, cfg):
    from issue_runner.phases.build import run_tests
    from tests.hardening_support import git

    faulty = (
        "def within_range(text, lower, upper):\n"
        "    return [n for n in map(int, text.split(',')) if lower <= n < upper]\n"
    )
    (git_repo / "range_impl.py").write_text(faulty)
    git(git_repo, "add", "range_impl.py")
    git(git_repo, "commit", "-m", "test: seed incomplete inclusive range")
    cfg.test_cmd = f"{sys.executable} -m pytest {{test_path}} -q"
    cfg.regression_cmd = f"{sys.executable} -m pytest -q"
    cfg.tester_retries = 0
    weak = (
        "from range_impl import within_range\n\n"
        "def test_interior():\n"
        "    assert within_range('1,3,5', 2, 4) == [3]\n"
    )
    strong = weak + (
        "\ndef test_upper_endpoint():\n    assert within_range('1,3,4,5', 2, 4) == [3,4]\n"
    )
    plan = json.dumps(
        {
            "summary": "filter inclusive bounds",
            "tickets": [
                {
                    "title": "Filter inclusive bounds",
                    "description": "Both endpoints must be included",
                    "test_assertion": "within_range('1,3,5', 2, 4) == [3]",
                }
            ],
        }
    )

    def repair():
        assert run_tests(cfg, "test_range.py")[0] is False
        (git_repo / "range_impl.py").write_text(faulty.replace("n < upper", "n <= upper"))

    client = FakeClient(
        [
            (plan, None),
            (
                '{"test_path":"test_range.py"}',
                lambda: (git_repo / "test_range.py").write_text(weak),
            ),
            (verdict("refine_test", tf="include the upper bound"), None),
            (
                '{"test_path":"test_range.py"}',
                lambda: (git_repo / "test_range.py").write_text(strong),
            ),
            ("fixed", repair),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert (report.done, report.blocked) == (1, 0)
    assert [c["role"] for c in client.calls] == [
        "planner",
        "builder.tester",
        "verifier",
        "builder.tester",
        "builder.coder",
        "verifier",
    ]
    assert "include the upper bound" in client.calls[3]["prompt"]
    assert run_tests(cfg, "test_range.py")[0] is True
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert store.tickets[0].rounds == 1
    assert store.tickets[0].already_satisfied is False


def test_initially_green_test_can_request_code_rework(git_repo, cfg):
    client = FakeClient(
        [
            *already_green_script(git_repo),
            (verdict("rework_code", cf="fix the missing behavior"), None),
            ("fixed", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert (report.done, report.blocked) == (1, 0)
    assert client.calls[-2]["role"] == "builder.coder"
    assert "fix the missing behavior" in client.calls[-2]["prompt"]


def test_retry_blocked_resets_and_reruns(git_repo, cfg):
    from issue_runner.tickets import Ticket

    store = TicketStore(git_repo / ".state", issue_ref="17")
    t = Ticket(id=1, title="subtract ints", description="d", test_assertion="a")
    t.status = "blocked"
    t.rounds = 3
    t.blocked_reason = "old reason"
    store.set_tickets([t])
    store.save()

    cfg.retry_blocked = True
    client = FakeClient(
        [
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.done == 1 and report.blocked == 0


class RecordingPullRequests:
    """Stands in for the gh CLI: records the push and the PR that would be opened."""

    def __init__(self):
        self.pushed = []
        self.created = []

    def push_branch(self, repo_dir, branch):
        self.pushed.append(branch)

    def open_pull_request(self, repo, head, title, body):
        self.created.append({"repo": repo, "head": head, "title": title, "body": body})
        return f"https://github.com/{repo}/pull/{len(self.created)}"


@pytest.fixture
def pull_requests(monkeypatch):
    from issue_runner import github_io
    from issue_runner.phases import devops

    fake = RecordingPullRequests()
    monkeypatch.setattr(devops, "push_branch", fake.push_branch)
    monkeypatch.setattr(github_io, "open_pull_request", fake.open_pull_request)
    return fake


def test_clean_run_opens_pull_request(git_repo, cfg, pull_requests):
    cfg.repo = "owner/repo"
    client = FakeClient(
        [
            (plan_reply(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
            ("done", implement(git_repo)),
            (verdict("pass"), None),
        ]
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.blocked == 0
    assert pull_requests.pushed == [report.branch]
    created = pull_requests.created[0]
    assert created["repo"] == "owner/repo"
    assert created["head"] == report.branch
    assert "#17" in created["title"] and "Add subtract" in created["title"]
    assert "Closes #17" in created["body"]
    assert "subtract ints" in created["body"]
    assert report.pr_url.endswith("/pull/1")
    assert any("pull request" in line for line in report.details)


def test_blocked_run_does_not_open_pull_request(git_repo, cfg, pull_requests):
    cfg.repo = "owner/repo"
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
    assert report.blocked == 1
    assert pull_requests.created == []
    assert report.pr_url == ""


def test_open_pr_disabled_skips_pull_request(git_repo, cfg, pull_requests):
    cfg.repo = "owner/repo"
    cfg.open_pr = False
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
    assert pull_requests.created == []


def test_plan_only_never_opens_pull_request(git_repo, cfg, pull_requests):
    cfg.repo = "owner/repo"
    client = FakeClient([(plan_reply(), None)])
    run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state", plan_only=True)
    assert pull_requests.created == [] and pull_requests.pushed == []


def test_push_failure_does_not_fail_the_run(git_repo, cfg, monkeypatch):
    from issue_runner.phases import devops
    from issue_runner.phases.devops import DevopsError

    def boom(repo_dir, branch):
        raise DevopsError("no origin remote")

    monkeypatch.setattr(devops, "push_branch", boom)
    cfg.repo = "owner/repo"
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
    assert report.pr_url == ""
    assert any("no origin remote" in line for line in report.details)


def test_budget_exhaustion_stops_the_run_and_saves_resumable_state(git_repo, cfg):
    from issue_runner.budget import BudgetExhausted

    cfg.max_run_credits = 3

    class BudgetedClient(FakeClient):
        """Affords the plan and the first ticket's tester, then runs dry."""

        def __init__(self, script, allowance):
            super().__init__(script)
            self.allowance = allowance

        def run(self, prompt, role, read_only=False, session_name=None):
            if len(self.calls) >= self.allowance:
                raise BudgetExhausted("run credit budget exhausted: 3/3 credits spent")
            return super().run(prompt, role, read_only, session_name)

    client = BudgetedClient(
        [
            (two_ticket_plan(), None),
            (json.dumps({"test_path": "test_sub.py"}), write_test(git_repo, "assert RED")),
        ],
        allowance=2,
    )
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")

    assert report.budget_exhausted is True
    assert report.done == 0
    # the active phase and the untouched second ticket are both resumable
    assert len(client.calls) == 2
    store = TicketStore(git_repo / ".state", issue_ref="17")
    assert store.load()
    assert report.blocked == 0
    assert store.tickets[0].status == "pending"
    assert store.tickets[0].phase == "coder"
    assert store.tickets[0].test_path == "test_sub.py"
    assert store.tickets[0].blocked_reason is None
    assert store.tickets[1].status == "pending"


def test_budget_exhaustion_does_not_start_later_tickets(git_repo, cfg):
    from issue_runner.budget import BudgetExhausted

    class ImmediatelyBrokeClient(FakeClient):
        def run(self, prompt, role, read_only=False, session_name=None):
            if role == "builder.tester":
                raise BudgetExhausted("run credit budget exhausted")
            return super().run(prompt, role, read_only, session_name)

    client = ImmediatelyBrokeClient([(two_ticket_plan(), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.budget_exhausted is True
    assert report.blocked == 0, "a budget pause must not block either ticket"
    # the raising override never records, so only the plan call is logged: the
    # second ticket's tester was never even attempted
    assert [c["role"] for c in client.calls] == ["planner"]
    store = TicketStore(git_repo / ".state", issue_ref="17")
    store.load()
    assert [t.status for t in store.tickets] == ["pending", "pending"]


def test_budget_exhaustion_skips_the_pull_request(git_repo, cfg, pull_requests):
    from issue_runner.budget import BudgetExhausted

    cfg.repo = "owner/repo"

    class BrokeClient(FakeClient):
        def run(self, prompt, role, read_only=False, session_name=None):
            if role == "builder.tester":
                raise BudgetExhausted("run credit budget exhausted")
            return super().run(prompt, role, read_only, session_name)

    client = BrokeClient([(plan_reply(), None)])
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state")
    assert report.budget_exhausted is True
    assert pull_requests.created == []


def two_ticket_plan():
    return json.dumps(
        {
            "summary": "two tickets",
            "tickets": [
                {"title": "a", "description": "d", "test_assertion": "a == 1"},
                {"title": "b", "description": "d", "test_assertion": "b == 2"},
            ],
        }
    )
