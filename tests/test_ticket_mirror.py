import json

import pytest

from issue_runner.tickets import Ticket
from issue_runner.trackers import (
    TrackerError,
    close_gitea_issue,
    create_gitea_subissue,
    gitea_write_token,
)


def make_ticket():
    return Ticket(id=1, title="sub task", description="do it", test_assertion="x == 1")


class RecordingRequester:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append({"method": method, "url": url, "token": token, "payload": payload})
        return self.responses.pop(0)


def test_create_gitea_subissue_posts_and_returns_number():
    requester = RecordingRequester([json.dumps({"number": 42})])
    number = create_gitea_subissue(
        "http://g:3000",
        "Org/repo",
        parent_number=1,
        ticket=make_ticket(),
        token="tok",
        requester=requester,
    )
    assert number == 42
    call = requester.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://g:3000/api/v1/repos/Org/repo/issues"
    assert call["payload"]["title"] == "[#1] sub task"
    assert "#1" in call["payload"]["body"]
    assert "x == 1" in call["payload"]["body"]


def test_close_gitea_issue_comments_then_closes():
    requester = RecordingRequester(["{}", "{}"])
    close_gitea_issue(
        "http://g:3000", "Org/repo", 42, "Done in abc", token="tok", requester=requester
    )
    comment, patch = requester.calls
    assert comment["method"] == "POST"
    assert comment["url"].endswith("/issues/42/comments")
    assert comment["payload"] == {"body": "Done in abc"}
    assert patch["method"] == "PATCH"
    assert patch["url"].endswith("/issues/42")
    assert patch["payload"] == {"state": "closed"}


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
