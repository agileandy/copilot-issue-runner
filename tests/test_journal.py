"""The intra-agent conversation must survive the process that produced it.

These tests pin the contract: a hand-back is written to the ticket, and the
next prompt sends the agent there to read it rather than carrying a copy.
"""

import json

import pytest

from issue_runner.journal import Journal
from issue_runner.phases.build import CoderFailure, coder_step, tester_step
from issue_runner.phases.verify import verify_step
from issue_runner.tickets import Ticket
from tests.conftest import FakeClient


class RecordingBackend:
    """A tracker that keeps every comment, like a real sub-issue thread."""

    def __init__(self, repo="owner/repo", fail=False):
        self.repo = repo
        self.fail = fail
        self.comments = []

    def comment(self, ticket, body):
        if self.fail:
            raise RuntimeError("github is down")
        self.comments.append((ticket.github_issue, body))

    def thread_ref(self, ticket):
        return f"gh issue view {ticket.github_issue} -R {self.repo} --comments"


def ticket(issue=77):
    return Ticket(
        id=1,
        title="add subtract",
        description="add a subtract function",
        test_assertion="subtract(5, 3) == 2",
        github_issue=issue,
    )


def write(repo, content, name="test_subtract.py"):
    def _side_effect():
        (repo / name).write_text(content)

    return _side_effect


REPLY = json.dumps({"test_path": "test_subtract.py"})


@pytest.fixture
def git_repo(repo):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "runner@test.local")
    git("config", "user.name", "Runner Test")
    git("add", "-A")
    git("commit", "-m", "seed")
    return repo


def write_sub(repo, content):
    return lambda: (repo / "test_sub.py").write_text(content)


def implement_sub(repo):
    return lambda: (repo / "impl.py").write_text("code")


def test_a_rejected_test_is_recorded_on_the_ticket(repo, cfg):
    """The reason a test was bounced belongs on the issue, not only in a prompt."""
    backend = RecordingBackend()
    t = ticket()
    client = FakeClient(
        [
            (REPLY, write(repo, "# nothing but a comment\n")),
            (REPLY, write(repo, "assert RED")),
        ]
    )

    assert tester_step(client, cfg, t, journal=Journal(backend)) == "test_subtract.py"

    assert len(backend.comments) == 1, "the rejection must be recorded exactly once"
    number, body = backend.comments[0]
    assert number == 77
    assert "harness → builder.tester" in body
    assert "only comments or blank lines" in body


def test_the_next_prompt_points_at_the_thread_instead_of_repeating_it(repo, cfg):
    """The ticket is the shared memory; the prompt is only a pointer to it."""
    backend = RecordingBackend()
    client = FakeClient(
        [
            (REPLY, write(repo, "# nothing but a comment\n")),
            (REPLY, write(repo, "assert RED")),
        ]
    )

    tester_step(client, cfg, ticket(), journal=Journal(backend))

    second = client.calls[1]["prompt"]
    assert "gh issue view 77 -R owner/repo --comments" in second
    assert "only comments or blank lines" not in second, (
        "the detail lives on the ticket, not in the prompt"
    )


def test_without_a_tracker_the_feedback_is_still_inlined(repo, cfg):
    """An offline run has no issue to read, so nothing may be lost."""
    client = FakeClient(
        [
            (REPLY, write(repo, "# nothing but a comment\n")),
            (REPLY, write(repo, "assert RED")),
        ]
    )

    tester_step(client, cfg, ticket(issue=None), journal=Journal(None))

    assert "only comments or blank lines" in client.calls[1]["prompt"]


def test_a_tracker_failure_downgrades_to_inline_rather_than_losing_feedback(repo, cfg):
    """A GitHub outage must cost visibility, never the agent's next instruction."""
    client = FakeClient(
        [
            (REPLY, write(repo, "# nothing but a comment\n")),
            (REPLY, write(repo, "assert RED")),
        ]
    )

    path = tester_step(client, cfg, ticket(), journal=Journal(RecordingBackend(fail=True)))

    assert path == "test_subtract.py"
    assert "only comments or blank lines" in client.calls[1]["prompt"]


def test_a_rejected_code_change_is_recorded_for_the_coder(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")
    backend = RecordingBackend()
    client = FakeClient([("done", None), ("done", None)])

    with pytest.raises(CoderFailure):
        coder_step(client, cfg, ticket(), "test_subtract.py", journal=Journal(backend))

    assert backend.comments, "the coder's failures must reach the ticket"
    assert all("harness → builder.coder" in body for _, body in backend.comments)


def test_the_verifier_is_told_where_the_history_is(repo, cfg):
    (repo / "test_subtract.py").write_text("assert PASS")
    verdict = json.dumps({"verdict": "pass", "reasons": ["fine"]})
    client = FakeClient([(verdict, None)])

    verify_step(client, cfg, ticket(), "test_subtract.py", journal=Journal(RecordingBackend()))

    assert "gh issue view 77 -R owner/repo --comments" in client.calls[0]["prompt"]


def test_the_verifier_prompt_is_honest_when_there_is_no_thread(repo, cfg):
    (repo / "test_subtract.py").write_text("assert PASS")
    verdict = json.dumps({"verdict": "pass", "reasons": ["fine"]})
    client = FakeClient([(verdict, None)])

    verify_step(client, cfg, ticket(issue=None), "test_subtract.py", journal=Journal(None))

    assert "no issue thread" in client.calls[0]["prompt"]


def test_a_message_names_the_speakers_and_the_round():
    t = ticket()
    t.rounds = 3
    assert Journal(RecordingBackend()).post(t, "verifier", "builder.coder", "wrong", "rework_code")
    body = Journal(RecordingBackend()).render(t, "x")
    assert "gh issue view" in body


def test_an_empty_message_is_not_posted():
    backend = RecordingBackend()
    assert Journal(backend).post(ticket(), "verifier", "builder.coder", "   ") is False
    assert backend.comments == []


def test_github_tickets_can_comment_and_be_read_back():
    from issue_runner.ticket_mirror import GithubTickets

    calls = []

    def fake_run(argv, capture_output=True, text=True):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    backend = GithubTickets("owner/repo")
    t = ticket(issue=91)
    assert backend.thread_ref(t) == "gh issue view 91 -R owner/repo --comments"

    import issue_runner.github_io as gio

    gio.comment_issue("owner/repo", 91, "hello", run=fake_run)
    assert calls[0][:6] == ["gh", "issue", "comment", "-R", "owner/repo", "91"]


def test_an_identical_repeat_is_not_posted_twice():
    """A retry loop repeating itself must not bury the thread agents are sent to."""
    backend = RecordingBackend()
    journal = Journal(backend)
    t = ticket()

    assert journal.post(t, "harness", "builder.coder", "test still fails") is True
    assert journal.post(t, "harness", "builder.coder", "test still fails") is True
    assert journal.post(t, "harness", "builder.coder", "something else") is True

    assert len(backend.comments) == 2, "only distinct messages reach the ticket"


def test_a_repeat_after_something_else_is_posted_again():
    backend = RecordingBackend()
    journal = Journal(backend)
    t = ticket()

    journal.post(t, "harness", "builder.coder", "A")
    journal.post(t, "harness", "builder.coder", "B")
    journal.post(t, "harness", "builder.coder", "A")

    assert len(backend.comments) == 3, "a later recurrence is real history, not noise"


def test_a_ticket_that_goes_right_first_time_is_still_documented(git_repo, cfg):
    """The defect this fixes: a clean run left the sub-issue completely empty.

    Only failures were recorded, so a ticket whose test went red then green on
    the first attempt produced no trace at all — the issue looked dead while
    the work was actually happening.
    """
    import subprocess

    from issue_runner.orchestrator import run_issue

    backend = RecordingBackend()
    backend.created = {}

    def create(parent_number, t):
        backend.created[t.id] = 900 + t.id
        return 900 + t.id

    def close(t, comment):
        backend.comments.append((t.github_issue, f"CLOSED: {comment}"))

    backend.create = create
    backend.close = close
    backend.block = lambda t, reason: None
    cfg.tickets_backend = backend

    client = FakeClient(
        [
            (
                json.dumps(
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
                ),
                None,
            ),
            (json.dumps({"test_path": "test_sub.py"}), write_sub(git_repo, "assert RED")),
            ("done", implement_sub(git_repo)),
            (json.dumps({"verdict": "pass", "reasons": ["robust"]}), None),
        ]
    )

    report = run_issue(cfg, client, {"number": 17, "title": "Add subtract", "body": "x", "url": ""})
    assert report.done == 1, "the ticket must actually complete"

    bodies = [b for _, b in backend.comments]
    handoffs = [b for b in bodies if "→" in b]
    assert len(handoffs) >= 4, f"every handoff must be recorded, got {len(handoffs)}: {bodies}"
    assert any("planner → builder.tester" in b for b in bodies), "the brief is missing"
    assert any("builder.tester → builder.coder" in b for b in bodies), "the spec handoff is missing"
    assert any("builder.coder → verifier" in b for b in bodies), "the review request is missing"
    assert any("verifier → harness" in b for b in bodies), "the passing verdict is missing"
    del subprocess


def test_the_recorded_brief_carries_the_assertion_the_work_is_judged_against(repo, cfg):
    from issue_runner.orchestrator import _brief_body
    from issue_runner.tickets import TicketStore

    store = TicketStore(repo, "17")
    store.branch = "issue-17-x"
    t = ticket()
    t.files_hint = ["src/range.py"]
    t.depends_on = [4]

    body = _brief_body(t, store)

    assert "subtract(5, 3) == 2" in body
    assert "src/range.py" in body
    assert "ticket 4" in body
    assert "issue-17-x" in body


def test_the_accepted_test_is_announced_as_a_frozen_specification():
    from issue_runner.orchestrator import _test_body

    body = _test_body(ticket(), "tests/test_range.py")

    assert "tests/test_range.py" in body
    assert "FAIL" in body, "the red-first proof is the point of the handoff"
    assert "frozen" in body


def test_an_already_green_test_is_announced_honestly():
    from issue_runner.orchestrator import _test_body

    t = ticket()
    t.already_satisfied = True

    assert "may already exist" in _test_body(t, "tests/test_range.py")


def test_the_coder_handoff_lists_what_changed_excluding_the_test(repo, cfg):
    import subprocess

    from issue_runner.orchestrator import _code_body

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    (repo / "impl.py").write_text("code")
    (repo / "test_range.py").write_text("assert RED")

    body = _code_body(cfg, "test_range.py")

    assert "impl.py" in body
    assert "- `test_range.py`" not in body, "the test is the spec, not part of the change"


def test_listing_the_changed_files_can_fail_without_costing_the_ticket(repo, cfg):
    """The handoff text is a record, not a gate — git failing must not abort."""
    from issue_runner.orchestrator import _code_body

    body = _code_body(cfg, "test_range.py")

    assert "now passes" in body
    assert "no source file changed" in body
