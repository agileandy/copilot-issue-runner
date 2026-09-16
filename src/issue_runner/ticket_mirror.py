"""Tracker backends for mirroring sub-task tickets.

The local state file is always the source of truth; a backend mirrors tickets
to the repo's tracker so progress is visible there.

- GitHub: one sub-issue per ticket, closed with a commit reference.
- Gitea: a markdown task checklist in the PARENT issue's description — Gitea
  renders checkbox progress in the issue list, with no sub-issue clutter. The
  runner owns only a marker-delimited section appended to the body; anything a
  human wrote above it is never touched. Lines are ticked as tickets land, and
  each completion adds a comment. Writes act as the claude bot (GITEA_CLAUDE_TOKEN).
"""

import json
import re

from . import github_io
from .trackers import _http_json, gitea_write_token

MARK_START = "<!-- issue-runner:tasks -->"
MARK_END = "<!-- /issue-runner:tasks -->"
_LINE_RE = re.compile(r"^- \[[ x]\] (\d+)\. (.*)$")


def upsert_task_line(
    body: str, ticket_id: int, title: str, done: bool, note: str | None = None
) -> str:
    """Add or update one checklist line inside the runner-owned section."""
    line = f"- [{'x' if done else ' '}] {ticket_id}. {title}"
    if note:
        line += f" — ⚠️ {note}"
    if MARK_START in body and MARK_END in body:
        head, rest = body.split(MARK_START, 1)
        section, tail = rest.split(MARK_END, 1)
        lines = [ln for ln in section.strip().splitlines() if ln.strip()]
        for i, existing in enumerate(lines):
            m = _LINE_RE.match(existing.strip())
            if m and int(m.group(1)) == ticket_id:
                lines[i] = line
                break
        else:
            lines.append(line)
        section = "\n".join(["### Sub-tasks", *[ln for ln in lines if ln != "### Sub-tasks"]])
        return f"{head}{MARK_START}\n{section}\n{MARK_END}{tail}"
    suffix = f"\n\n{MARK_START}\n### Sub-tasks\n{line}\n{MARK_END}"
    return body.rstrip("\n") + suffix if body else suffix.lstrip("\n")


class GithubTickets:
    def __init__(self, repo: str):
        self.repo = repo

    def create(self, parent_number: int, ticket) -> int:
        return github_io.create_subissue(self.repo, parent_number, ticket)

    def close(self, ticket, comment: str) -> None:
        github_io.close_subissue(self.repo, ticket.github_issue, comment)

    def block(self, ticket, reason: str) -> None:
        github_io.comment_issue(self.repo, ticket.github_issue, f"BLOCKED: {reason}")

    def comment(self, ticket, body: str) -> None:
        github_io.comment_issue(self.repo, ticket.github_issue, body)

    def thread_ref(self, ticket) -> str:
        """How an agent reads this ticket's transcript from inside the worktree."""
        return f"gh issue view {ticket.github_issue} -R {self.repo} --comments"


class GiteaTickets:
    def __init__(self, api_base: str, owner_repo: str, requester=_http_json):
        self.api_base = api_base.rstrip("/")
        self.owner_repo = owner_repo
        self.requester = requester

    def _issue_url(self, number: int) -> str:
        return f"{self.api_base}/api/v1/repos/{self.owner_repo}/issues/{number}"

    def _update_line(self, parent_number: int, ticket, done: bool, note: str | None = None) -> None:
        token = gitea_write_token()
        issue = json.loads(self.requester("GET", self._issue_url(parent_number), token))
        body = upsert_task_line(issue.get("body") or "", ticket.id, ticket.title, done, note)
        self.requester("PATCH", self._issue_url(parent_number), token, {"body": body})

    def create(self, parent_number: int, ticket) -> int:
        self._update_line(parent_number, ticket, done=False)
        return parent_number  # the parent issue is the tracker ref for checklist mode

    def close(self, ticket, comment: str) -> None:
        parent_number = ticket.github_issue
        self._update_line(parent_number, ticket, done=True)
        self._comment(parent_number, f"Sub-task {ticket.id} ({ticket.title}): {comment}")

    def block(self, ticket, reason: str) -> None:
        parent_number = ticket.github_issue
        short = reason.splitlines()[0][:80].rstrip()
        self._update_line(parent_number, ticket, done=False, note=f"blocked: {short}")
        self._comment(parent_number, f"Sub-task {ticket.id} ({ticket.title}) BLOCKED: {reason}")

    def _comment(self, parent_number: int, body: str) -> None:
        self.requester(
            "POST",
            f"{self._issue_url(parent_number)}/comments",
            gitea_write_token(),
            {"body": body},
        )

    def comment(self, ticket, body: str) -> None:
        """Record the transcript on the parent issue, since there is no sub-issue.

        Deliberately no `thread_ref`: Gitea gives the agent no way to read this
        back, so the journal keeps inlining feedback into the prompt and this
        thread serves the human reader only.
        """
        self._comment(ticket.github_issue, f"Sub-task {ticket.id} ({ticket.title})\n\n{body}")
