import json
from pathlib import Path

import pytest

from issue_runner import plan_issue
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


def test_comment_issue_posts_the_body_unchanged():
    api = FakeGiteaApi()
    GiteaTickets("http://g:3000", "Org/repo", requester=api).comment_issue(1, "run summary")
    assert api.comments == ["run summary"]


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


def test_github_create_links_new_ticket_as_subissue_of_parent(monkeypatch):
    import issue_runner.github_io as gio
    from issue_runner.ticket_mirror import GithubTickets

    links = []
    monkeypatch.setattr(gio, "create_subissue", lambda repo, parent, ticket: 42)
    monkeypatch.setattr(gio, "link_subissue", lambda repo, parent, child: links.append((repo, parent, child)))

    number = GithubTickets("owner/repo").create(17, make_ticket())

    assert (number, links) == (42, [("owner/repo", 17, 42)])


def test_github_create_logs_failed_subissue_link_and_returns_ticket(monkeypatch, caplog):
    import issue_runner.github_io as gio
    from issue_runner.ticket_mirror import GithubTickets

    def failing_link(repo, parent, child):
        raise gio.GithubError("HTTP 422")

    monkeypatch.setattr(gio, "create_subissue", lambda repo, parent, ticket: 42)
    monkeypatch.setattr(gio, "link_subissue", failing_link)

    with caplog.at_level("WARNING", logger="issue_runner"):
        number = GithubTickets("owner/repo").create(17, make_ticket())

    assert (
        number == 42
        and "could not link #42 as a sub-issue of #17" in caplog.text
        and "HTTP 422" in caplog.text
    )


# --- plan issue body mirror (--plan-from-issue) ---

PLAN = (Path(__file__).parent / "fixtures" / "plan_gh_issue.md").read_text()
T1 = "- [ ] Add TokenBucket — assert: `TokenBucket(2).take()` returns False on the third call"
T2 = (
    "- [ ] Refill over time — assert: after one refill interval `TokenBucket(2).take()` "
    "returns True again"
)


def _tickets():
    return plan_issue.parse(PLAN).tickets


def test_plan_rewrite_links_sub_issues_and_changes_nothing_else():
    tickets = _tickets()
    for ticket, number in zip(tickets, (12, 13, 14)):
        ticket.github_issue = number

    body = plan_issue.rewrite(PLAN, tickets)

    expected = PLAN.replace(T1, T1 + " · #12").replace(T2, T2 + " · #13")
    expected = expected.replace("status 429\n", "status 429 · #14\n")
    assert body == expected


def test_plan_rewrite_ticks_a_done_ticket():
    tickets = _tickets()
    tickets[0].status = "done"

    body = plan_issue.rewrite(PLAN, tickets)

    assert T1.replace("- [ ]", "- [x]") + "\n" in body
    assert "### Phase 1: Limiter · `wip`" in body


def test_plan_rewrite_shows_wip_and_failed_with_the_reason_last():
    tickets = _tickets()
    tickets[0].status = "in_progress"
    tickets[1].status = "blocked"
    tickets[1].blocked_reason = "no clock seam\nmore detail"

    body = plan_issue.rewrite(PLAN, tickets)

    assert T1.replace("- [ ] ", "- [ ] wip ") + "\n" in body
    assert T2.replace("- [ ] ", "- [ ] failed ") + " — failed: no clock seam\n" in body
    assert "### Phase 1: Limiter · `failed`" in body


def test_plan_rewrite_marks_a_phase_done_once_its_tickets_and_gate_pass():
    tickets = _tickets()
    tickets[0].status = tickets[1].status = "done"
    gates = {(1, "uv run pytest tests/test_limiter.py"): "pass"}

    body = plan_issue.rewrite(PLAN, tickets, gates)

    assert "### Phase 1: Limiter · `done`" in body
    assert "- [x] `uv run pytest tests/test_limiter.py` — the bucket works" in body
    assert "### Phase 2: Wire into search · `idle`" in body


def test_plan_rewrite_round_trips_through_parse():
    tickets = _tickets()
    tickets[0].status, tickets[0].github_issue = "done", 12

    again = plan_issue.parse(plan_issue.rewrite(PLAN, tickets))

    assert again.fingerprint == plan_issue.parse(PLAN).fingerprint
    assert [t.status for t in again.tickets] == ["done", "pending", "pending"]


def test_plan_body_mirror_writes_only_when_the_body_changes():
    state = {"body": PLAN}
    edits = []

    def edit(body):
        edits.append(body)
        state["body"] = body

    mirror = plan_issue.PlanBodyMirror("o/r", 7, fetch=lambda: state["body"], edit=edit)
    tickets = _tickets()
    tickets[0].status = "done"

    assert mirror.sync(tickets) is True
    assert mirror.sync(tickets) is False
    assert len(edits) == 1


def test_edit_issue_body_sends_the_body_on_stdin():
    from issue_runner import github_io

    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kwargs):
        seen.update(argv=argv, input=kwargs.get("input"))
        return Done()

    github_io.edit_issue_body("o/r", 7, "new body", run=run)

    assert seen["argv"][:4] == ["gh", "issue", "edit", "7"]
    assert "--body-file" in seen["argv"] and seen["input"] == "new body"


def test_plan_body_mirror_keeps_an_edit_made_while_it_was_rewriting():
    state = {"body": PLAN, "reads": 0}

    def fetch():
        state["reads"] += 1
        if state["reads"] == 2:  # a person edits the plan between our read and our write
            state["body"] = state["body"].replace("Rate limit search.", "Rate limit search, gently.")
        return state["body"]

    mirror = plan_issue.PlanBodyMirror(
        "o/r", 7, fetch=fetch, edit=lambda new: state.update(body=new)
    )
    tickets = _tickets()
    tickets[0].status = "done"

    assert mirror.sync(tickets) is True
    assert "Rate limit search, gently." in state["body"]
    assert T1.replace("- [ ]", "- [x]") + "\n" in state["body"]
