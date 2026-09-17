"""A sub-task must show on the tracker that it is being worked on, then that it is done.

Before this, a sub-issue sat open and unmarked for the whole ticket, so GitHub
gave no way to tell work-in-progress from work not yet started.
"""

import json

import pytest

from issue_runner.orchestrator import run_issue
from issue_runner.ticket_mirror import IN_PROGRESS_LABEL, GithubTickets
from tests.conftest import FakeClient


class StatusBackend:
    """Records the order of tracker calls, which is the thing under test."""

    def __init__(self, fail_close=False):
        self.events = []
        self.fail_close = fail_close

    def create(self, parent_number, ticket):
        self.events.append(("create", ticket.id))
        return 900 + ticket.id

    def start(self, ticket):
        self.events.append(("start", ticket.id))

    def close(self, ticket, comment):
        if self.fail_close:
            raise RuntimeError("github is down")
        self.events.append(("close", ticket.id))

    def block(self, ticket, reason):
        self.events.append(("block", ticket.id))

    def comment(self, ticket, body):
        pass


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


ISSUE = {"number": 17, "title": "Add subtract", "body": "need it", "url": ""}


def two_ticket_plan():
    return json.dumps(
        {
            "summary": "two tickets",
            "tickets": [
                {"title": "one", "description": "first", "test_assertion": "a == 1"},
                {"title": "two", "description": "second", "test_assertion": "b == 2"},
            ],
        }
    )


def one_ticket_plan():
    return json.dumps(
        {
            "summary": "one ticket",
            "tickets": [{"title": "one", "description": "first", "test_assertion": "a == 1"}],
        }
    )


def ticket_script(repo, name):
    return [
        (
            json.dumps({"test_path": f"test_{name}.py"}),
            lambda: (repo / f"test_{name}.py").write_text("assert RED"),
        ),
        ("done", lambda: (repo / "impl.py").write_text("code")),
        (json.dumps({"verdict": "pass", "reasons": ["ok"]}), None),
    ]


def test_a_ticket_is_marked_in_progress_then_done_before_the_next_one_starts(git_repo, cfg):
    backend = StatusBackend()
    cfg.tickets_backend = backend
    client = FakeClient(
        [
            (two_ticket_plan(), None),
            *ticket_script(git_repo, "one"),
            *ticket_script(git_repo, "two"),
        ]
    )

    report = run_issue(cfg, client, ISSUE)
    assert report.done == 2, "both tickets must complete"

    lifecycle = [e for e in backend.events if e[0] in ("start", "close")]
    assert lifecycle == [("start", 1), ("close", 1), ("start", 2), ("close", 2)], (
        f"a ticket must be marked done before the next one is marked in progress, got {lifecycle}"
    )


def test_a_blocked_ticket_is_not_left_looking_in_progress(git_repo, cfg):
    backend = StatusBackend()
    cfg.tickets_backend = backend
    # the tester keeps naming a file it never writes, so the ticket is blocked
    client = FakeClient(
        [(one_ticket_plan(), None), *[(json.dumps({"test_path": "test_one.py"}), None)] * 3]
    )

    run_issue(cfg, client, ISSUE)

    assert ("start", 1) in backend.events
    assert ("block", 1) in backend.events


def test_a_tracker_that_refuses_the_status_does_not_stop_the_work(git_repo, cfg):
    """The status is a light on the wall, not a gate."""

    class Refusing(StatusBackend):
        def start(self, ticket):
            raise RuntimeError("no permission to label")

    cfg.tickets_backend = Refusing()
    client = FakeClient([(one_ticket_plan(), None), *ticket_script(git_repo, "one")])

    report = run_issue(cfg, client, ISSUE)

    assert report.done >= 1, "the ticket must still be built and committed"


def test_a_committed_ticket_survives_a_failed_close(git_repo, cfg):
    cfg.tickets_backend = StatusBackend(fail_close=True)
    client = FakeClient([(one_ticket_plan(), None), *ticket_script(git_repo, "one")])

    report = run_issue(cfg, client, ISSUE)

    assert report.done >= 1
    assert any("could not be closed" in d for d in report.details), (
        "a tracker failure must be reported, not silently swallowed"
    )


def test_github_marks_start_with_a_label_and_creates_it_once():
    calls = []

    def fake_run(argv, capture_output=True, text=True):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    import issue_runner.github_io as gio

    original = gio._run
    gio._run = lambda argv, run=None: fake_run(argv)
    try:
        backend = GithubTickets("owner/repo")

        class T:
            github_issue = 91
            id = 1

        backend.start(T())
        backend.start(T())
    finally:
        gio._run = original

    creates = [c for c in calls if c[:2] == ["gh", "label"]]
    adds = [c for c in calls if "--add-label" in c]
    assert len(creates) == 1, "the label is ensured once per run, not once per ticket"
    assert len(adds) == 2
    assert adds[0][-1] == IN_PROGRESS_LABEL


def test_a_finished_subissue_is_closed_as_completed_without_the_label():
    calls = []

    import issue_runner.github_io as gio

    original = gio._run
    gio._run = lambda argv, run=None: calls.append(argv)
    try:

        class T:
            github_issue = 91
            id = 1

        GithubTickets("owner/repo").close(T(), "done")
    finally:
        gio._run = original

    assert any("--remove-label" in c for c in calls), "the in-progress label must be cleared"
    close = next(c for c in calls if c[:3] == ["gh", "issue", "close"])
    assert "completed" in close, "a proven ticket must not look abandoned"
