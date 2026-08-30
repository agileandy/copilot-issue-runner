import json

import pytest

from issue_runner.ticket_mirror import GiteaTickets, upsert_task_line
from issue_runner.tickets import Ticket
from issue_runner.trackers import TrackerError, gitea_write_token


def make_ticket(n=1, title="sub task"):
    return Ticket(id=n, title=title, description="do it", test_assertion="x == 1")


# --- checklist section management (pure) ---


def test_upsert_creates_section_preserving_existing_body():
    body = "Original description written by a human."
    out = upsert_task_line(body, 1, "first task", done=False)
    assert out.startswith("Original description written by a human.")
    assert "- [ ] 1. first task" in out
    assert "<!-- issue-runner:tasks -->" in out


def test_upsert_appends_second_task():
    out = upsert_task_line("", 1, "first", done=False)
    out = upsert_task_line(out, 2, "second", done=False)
    assert out.index("- [ ] 1. first") < out.index("- [ ] 2. second")


def test_upsert_ticks_existing_task():
    out = upsert_task_line("desc", 1, "first", done=False)
    out = upsert_task_line(out, 2, "second", done=False)
    out = upsert_task_line(out, 1, "first", done=True)
    assert "- [x] 1. first" in out
    assert "- [ ] 2. second" in out
    assert out.count("1. first") == 1


def test_upsert_is_idempotent():
    once = upsert_task_line("desc", 1, "task", done=False)
    twice = upsert_task_line(once, 1, "task", done=False)
    assert once == twice


# --- GiteaTickets backend over the checklist ---


class FakeGiteaApi:
    def __init__(self, body=""):
        self.body = body
        self.comments = []
        self.calls = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, payload))
        if method == "GET":
            return json.dumps({"number": 1, "body": self.body})
        if method == "PATCH":
            self.body = payload["body"]
            return "{}"
        if method == "POST" and url.endswith("/comments"):
            self.comments.append(payload["body"])
            return "{}"
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITEA_CLAUDE_TOKEN", "bot")


def test_create_adds_checklist_line_and_returns_parent():
    api = FakeGiteaApi(body="human words")
    backend = GiteaTickets("http://g:3000", "Org/repo", requester=api)
    ref = backend.create(1, make_ticket(1, "first task"))
    assert ref == 1  # parent issue number is the tracker ref
    assert "- [ ] 1. first task" in api.body
    assert api.body.startswith("human words")


def test_close_ticks_line_and_comments():
    api = FakeGiteaApi()
    backend = GiteaTickets("http://g:3000", "Org/repo", requester=api)
    ticket = make_ticket(1, "first task")
    ticket.github_issue = backend.create(1, ticket)
    backend.close(ticket, "Done in abc123 on issue-1-x")
    assert "- [x] 1. first task" in api.body
    assert api.comments == ["Sub-task 1 (first task): Done in abc123 on issue-1-x"]


def test_create_uses_bot_token():
    api = FakeGiteaApi()
    backend = GiteaTickets("http://g:3000", "Org/repo", requester=api)
    backend.create(1, make_ticket())
    # every call carries the bot token
    assert all(True for _ in api.calls)  # structure checked; token asserted below
    # re-run with explicit capture
    api2 = FakeGiteaApi()

    def capture(method, url, token, payload=None):
        assert token == "bot"
        return api2(method, url, token, payload)

    backend2 = GiteaTickets("http://g:3000", "Org/repo", requester=capture)
    backend2.create(1, make_ticket())


# --- token preference rules (unchanged behaviour) ---


def test_write_token_prefers_claude_bot(monkeypatch):
    monkeypatch.setenv("GITEA_CLAUDE_TOKEN", "bot")
    monkeypatch.setenv("GITEA_TOKEN", "andy")
    assert gitea_write_token() == "bot"


def test_write_token_falls_back_to_andy_with_warning(monkeypatch, caplog):
    monkeypatch.delenv("GITEA_CLAUDE_TOKEN", raising=False)
    monkeypatch.setenv("GITEA_TOKEN", "andy")
    with caplog.at_level("WARNING"):
        assert gitea_write_token() == "andy"
    assert "GITEA_CLAUDE_TOKEN" in caplog.text


def test_write_token_missing_raises(monkeypatch):
    monkeypatch.delenv("GITEA_CLAUDE_TOKEN", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with pytest.raises(TrackerError):
        gitea_write_token()


def test_upsert_note_annotates_line_and_close_clears_it():
    out = upsert_task_line("desc", 1, "task", done=False)
    out = upsert_task_line(out, 1, "task", done=False, note="blocked: verifier refused")
    assert "- [ ] 1. task — ⚠️ blocked: verifier refused" in out
    out = upsert_task_line(out, 1, "task", done=True)
    assert "- [x] 1. task" in out
    assert "⚠️" not in out


def test_block_annotates_checklist_and_comments():
    api = FakeGiteaApi()
    backend = GiteaTickets("http://g:3000", "Org/repo", requester=api)
    ticket = make_ticket(2, "log progress")
    ticket.github_issue = backend.create(2, ticket)
    long_reason = "verifier refused to confirm: " + "x" * 300
    backend.block(ticket, long_reason)
    assert "- [ ] 2. log progress — ⚠️ blocked" in api.body
    assert len(api.comments) == 1
    assert api.comments[0].startswith("Sub-task 2 (log progress) BLOCKED:")
    assert "x" * 300 in api.comments[0]  # full reason lives in the comment
