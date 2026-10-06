"""Review: when has the PR passed code review, and what must be fixed if not.

`evaluate` is the gate. It is a pure function of GitHub data so it can be
tested exhaustively, and it never asks an agent: the review passes only when

- every check run and commit status on the head has finished, and none failed,
- the review bot's latest review is on the head and recommends approval,
- no review thread is left unresolved,
- no person's latest review requests changes, and
- the branch's required approvals are present.

Anything that failed is a finding for the reviser. Anything still running is
something to wait for.

`revise` hands the findings to the `reviser` agent and holds its change to the
same guards as a ticket: frozen tests and Dev checks untouched, no commits or
branch moves by the agent, every ticket test and the regression suite green.
"""

import base64
import hashlib
import re
from dataclasses import dataclass, field

from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json_object
from ..testcmd import detect_regression_cmd
from ..tickets import TicketStore
from . import acceptance, devops
from .build import BuildError, resolve_test_path, run_test_command, run_tests

OK_CONCLUSIONS = ("success", "neutral", "skipped")
APPROVAL = "Approval recommended"
_VERDICT = re.compile(r"^###\s+(?:\S+\s+)?(.+?)\s*$", re.MULTILINE)


class ReviewError(RuntimeError):
    pass


@dataclass
class Finding:
    kind: str  # "thread", "check", "status", "review", "changes_requested"
    ref: str  # thread id, check name, status context or review id
    where: str
    text: str


@dataclass
class ReviewStatus:
    state: str  # "pass", "pending" or "findings"
    waiting_for: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def login(user: dict | None) -> str:
    return ((user or {}).get("login") or "").removesuffix("[bot]")


def verdict(body: str) -> tuple[str | None, str]:
    """The overview verdict of a Copilot review and the sentence that explains it."""
    match = _VERDICT.search(body or "")
    if not match:
        return None, ""
    rest = body[match.end() :].strip().split("\n\n", 1)[0].strip()
    return match.group(1), rest


def evaluate(
    *,
    head_sha: str,
    checks: list[dict],
    statuses: list[dict],
    reviews: list[dict],
    threads: list[dict],
    bot: str,
    required_approvals: int,
) -> ReviewStatus:
    waiting: list[str] = []
    findings: list[Finding] = []

    for check in checks:
        name = check.get("name", "check")
        if check.get("status") != "completed":
            waiting.append(f"check {name}")
        elif check.get("conclusion") not in OK_CONCLUSIONS:
            output = check.get("output") or {}
            detail = output.get("title") or output.get("summary") or ""
            findings.append(
                Finding(
                    "check",
                    str(check.get("id", name)),
                    name,
                    f"{check.get('conclusion')}: {detail}".strip(": "),
                )
            )
    for status in statuses:
        context = status.get("context", "status")
        if status.get("state") == "pending":
            waiting.append(f"status {context}")
        elif status.get("state") != "success":
            findings.append(
                Finding("status", context, context, status.get("description") or status["state"])
            )

    bot = bot.removesuffix("[bot]")
    by_bot = [r for r in reviews if login(r.get("user")) == bot]
    latest = by_bot[-1] if by_bot else None
    if latest is None or latest.get("commit_id") != head_sha:
        waiting.append(f"a {bot} review of {head_sha[:8]}")
    else:
        found, why = verdict(latest.get("body", ""))
        if found is not None and found != APPROVAL:
            findings.append(Finding("review", str(latest.get("id")), bot, f"{found}: {why}"))

    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = (thread.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        where = f"{thread.get('path')}:{thread.get('line')}" if thread.get("path") else "PR"
        findings.append(
            Finding(
                "thread",
                thread["id"],
                where,
                f"{login(first.get('author'))}: {first.get('body', '')}",
            )
        )

    latest_human: dict[str, dict] = {}
    for review in reviews:
        who = login(review.get("user"))
        is_person = who and who != bot and (review.get("user") or {}).get("type") != "Bot"
        if is_person and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest_human[who] = review
    approvals = 0
    for who, review in latest_human.items():
        if review["state"] == "CHANGES_REQUESTED":
            findings.append(
                Finding("changes_requested", str(review.get("id")), who, review.get("body") or "")
            )
        elif review["state"] == "APPROVED":
            approvals += 1
    if approvals < required_approvals:
        waiting.append(f"{required_approvals - approvals} more approval(s)")

    if waiting:
        return ReviewStatus("pending", waiting, findings)
    return ReviewStatus("findings" if findings else "pass", [], findings)


# --- the reviser ---------------------------------------------------------------

REVISER_PROMPT = """\
You are the reviser in an automated delivery pipeline working on this repository.

Pull request #{pr} implements GitHub issue #{number}: {title}

{body}

Code review and CI raised these findings on the current head:
{findings}

Fix every finding that is right, in the working tree, following the
repository's conventions. A review comment that is wrong or does not apply may
be declined with a reason; a second reviewer will check that reason.

HARD RULES:
- Do NOT modify these test files; they are the accepted specification:
{tests}
- Do not commit, push, switch branches or rewrite history. The runner commits.
- Keep the change minimal and focused on the findings.

Reply with ONLY this JSON (no prose):
{{
  "threads": [{{"ref": "T1", "action": "fixed" | "not_applicable", "reason": "<one line>"}}],
  "notes": "<one line about the whole change>"
}}
{feedback}"""

AGREE_PROMPT = """\
You are a second reviewer in an automated delivery pipeline. You have read-only access.

A code review comment on {where} said:
{comment}

The author declined to change the code, saying:
{reason}

Read the code. Is declining right: is the comment wrong or not applicable here?

Reply with ONLY this JSON: {{"agree": true | false, "reason": "<one line>"}}"""


@dataclass
class Revision:
    changed: list[str]
    actions: dict[str, tuple[str, str]]  # finding ref -> (action, reason)
    notes: str = ""


def describe(findings: list[Finding]) -> tuple[str, dict[str, Finding]]:
    """Number the findings T1.. for the agent; return the text and the label map."""
    labels: dict[str, Finding] = {}
    lines = []
    for i, finding in enumerate(findings, start=1):
        label = f"T{i}"
        labels[label] = finding
        lines.append(f"{label} [{finding.kind}] {finding.where}: {finding.text}")
    return "\n".join(lines), labels


def revise(
    client, cfg: RunnerConfig, issue: dict, store: TicketStore, pr: int, findings: list[Finding]
) -> Revision:
    text, labels = describe(findings)
    tests = [t for t in store.tickets if t.test_hash and t.test_path]
    prompt_args = {
        "pr": pr,
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "findings": text,
        "tests": "\n".join(f"  - {t.test_path}" for t in tests) or "  (none)",
    }
    head = devops.head_commit(cfg.repo_dir)
    extra = ""
    last_error = "no attempt"
    for _ in range(cfg.coder_retries + 1):
        reply = client.run(
            REVISER_PROMPT.format(**prompt_args, feedback=extra),
            role="reviser",
            session_name=f"reviser-pr{pr}",
        )
        if devops.current_branch(cfg.repo_dir) != store.branch:
            raise ReviewError("the reviser changed the run branch")
        if devops.head_commit(cfg.repo_dir) != head:
            raise ReviewError("the reviser moved HEAD; only the runner commits")
        _require_frozen_checks(store)
        restored = _restore_changed_tests(cfg, tests)
        if restored:
            last_error = (
                f"you modified accepted test files {', '.join(restored)}; they were restored"
            )
        else:
            try:
                actions, notes = _parse_actions(reply, labels)
            except (JsonExtractError, ReviewError, TypeError) as e:
                actions, notes, last_error = None, "", f"the reply was not the required JSON: {e}"
            if actions is not None:
                changed = devops.changed_paths(cfg.repo_dir)
                problem = _verify_change(cfg, tests, changed, actions, labels)
                if problem is None:
                    return Revision(changed, actions, notes)
                last_error = problem
        extra = f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED (fix this):\n{last_error}"
    raise ReviewError(f"the reviser could not produce an acceptable change: {last_error}")


def agrees(client, finding: Finding, reason: str) -> tuple[bool, str]:
    """A read-only second opinion on declining a review comment."""
    reply = client.run(
        AGREE_PROMPT.format(where=finding.where, comment=finding.text, reason=reason),
        role="verifier",
        read_only=True,
        session_name="review-arbiter",
    )
    try:
        data = extract_json_object(reply)
    except (JsonExtractError, TypeError):
        return False, "the second reviewer gave no usable answer"
    return data.get("agree") is True, str(data.get("reason", ""))


def _parse_actions(reply: str, labels: dict[str, Finding]):
    data = extract_json_object(reply)
    raw = data.get("threads") or []
    if not isinstance(raw, list):
        raise ReviewError('"threads" must be a list')
    actions: dict[str, tuple[str, str]] = {}
    for entry in raw:
        label = entry.get("ref") if isinstance(entry, dict) else None
        if label not in labels:
            raise ReviewError(f"unknown finding {label!r}")
        if entry.get("action") not in ("fixed", "not_applicable"):
            raise ReviewError(f'{label} needs "action": "fixed" or "not_applicable"')
        actions[labels[label].ref] = (entry["action"], str(entry.get("reason", "")))
    return actions, str(data.get("notes", ""))


def _verify_change(cfg, tests, changed, actions, labels) -> str | None:
    if not changed:
        if any(action == "fixed" for action, _ in actions.values()):
            return "you reported findings as fixed but changed no file"
        if any(f.kind != "thread" for f in labels.values()):
            return "failing checks or review verdicts need a change, but no file changed"
        return None
    for ticket in tests:
        try:
            passed, output = run_tests(cfg, ticket.test_path)
        except BuildError as e:
            return f"ticket {ticket.id}'s test {ticket.test_path} no longer runs: {e}"
        if not passed:
            return f"ticket {ticket.id}'s test {ticket.test_path} now fails:\n{output[-1500:]}"
    command = cfg.regression_cmd or detect_regression_cmd(cfg.repo_dir)
    if not command:
        return "no regression command is configured or detectable"
    try:
        passed, output = run_test_command(cfg, command)
    except BuildError as e:
        return f"the regression suite did not run: {e}"
    if not passed:
        return f"the regression suite fails:\n{output[-2000:]}"
    return None


def _restore_changed_tests(cfg, tests) -> list[str]:
    restored = []
    for ticket in tests:
        path = resolve_test_path(cfg, ticket.test_path)
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if current != ticket.test_hash:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(ticket.test_snapshot))
            restored.append(ticket.test_path)
    return restored


def _require_frozen_checks(store: TicketStore) -> None:
    for criterion in store.delivery.criteria if store.delivery else []:
        if criterion.get("check_hash"):
            try:
                acceptance.frozen_check(criterion)
            except acceptance.AcceptError as e:
                raise ReviewError(str(e)) from e
