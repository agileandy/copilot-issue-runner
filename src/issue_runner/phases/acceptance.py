"""Acceptance: does the finished change meet the issue's acceptance criteria?

Before a PR opens, a read-only acceptor agent judges every criterion that is
proven by tests. Its verdict is advisory until the harness checks it: a "met"
counts only when it cites at least one test file that exists in the worktree
and passes when the harness runs it. Anything else is "unmet".

Criteria that can only be observed in Dev get an executable check instead,
written before merge by `acceptance.tester` and run through the repository's
`dev_check_cmd`. Like a ticket test it must fail first: run against Dev before
the change is deployed it must print ACCEPT-FAIL, or it proves nothing. The
runner saves the check outside the worktree and freezes it by hash, so it is
never committed and cannot be weakened before it runs again after deployment.
"""

import hashlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..agent_rules import workspace_rules
from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json_object
from ..tickets import TicketStore
from . import devops
from .build import BuildError, resolve_test_path, run_tests

VERDICTS = ("met", "unmet")

ACCEPT_PROMPT = """\
You are the acceptor in an automated delivery pipeline working on this repository.
You have read-only access, but you MAY run the tests via shell commands.

GitHub issue #{number}: {title}

{body}

The change for this issue is complete on this branch. Its diff against the base:
{diff}

Tickets built for it, and the test that proves each one:
{tickets}

Judge each of these acceptance criteria against the change and its tests:
{criteria}

A criterion is "met" only when the change implements it AND at least one test
in this repository proves it and passes. Cite those tests by file path relative
to the repository root. Never cite a test that does not exist. If no test
proves a criterion, it is "unmet": say what is missing.

{rules}
Reply with ONLY this JSON (no prose):
{{
  "criteria": [
    {{"id": "AC1", "verdict": "met" | "unmet", "tests": ["<test file path>"],
      "reason": "<one line>"}}
  ]
}}
{feedback}"""


class AcceptError(RuntimeError):
    pass


def judge(client, cfg: RunnerConfig, issue: dict, store: TicketStore, criteria: list[dict]):
    """The acceptor's verdict for each criterion, checked by the harness.

    Returns one dict per criterion: id, verdict ("met"|"unmet"), tests, reason.
    """
    ids = [c["id"] for c in criteria]
    prompt_args = {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "diff": devops.diff_since(cfg.repo_dir, store.initial_head),
        "tickets": "\n".join(
            f"{t.id}. {t.title} — test: {t.test_path or 'none'} — criteria: "
            f"{', '.join(t.criteria) or 'none'}"
            for t in store.tickets
        ),
        "criteria": "\n".join(f"{c['id']}: {c['text']}" for c in criteria),
        "rules": workspace_rules(cfg),
    }
    extra = ""
    last_error = "no attempt"
    for _ in range(2):
        reply = client.run(
            ACCEPT_PROMPT.format(**prompt_args, feedback=extra),
            role="acceptor",
            read_only=True,
            session_name="acceptor",
        )
        try:
            verdicts = _parse(reply, ids)
        except (JsonExtractError, AcceptError, TypeError) as e:
            last_error = str(e)
            extra = f"\nYOUR PREVIOUS REPLY WAS INVALID (fix this):\n{last_error}"
            continue
        return [_check(cfg, v) for v in verdicts]
    raise AcceptError(f"acceptor failed: {last_error}")


def _parse(reply: str, ids: list[str]) -> list[dict]:
    data = extract_json_object(reply)
    raw = data.get("criteria")
    if not isinstance(raw, list):
        raise AcceptError('the reply has no "criteria" list')
    found: dict[str, dict] = {}
    for entry in raw:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if cid not in ids:
            raise AcceptError(f"unknown criterion {cid!r}")
        if cid in found:
            raise AcceptError(f"criterion {cid} is judged twice")
        if entry.get("verdict") not in VERDICTS:
            raise AcceptError(f'criterion {cid} needs "verdict": "met" or "unmet"')
        tests = entry.get("tests") or []
        if not isinstance(tests, list):
            raise AcceptError(f'criterion {cid}: "tests" must be a list')
        found[cid] = {
            "id": cid,
            "verdict": entry["verdict"],
            "tests": [str(t) for t in tests],
            "reason": str(entry.get("reason", "")),
        }
    missing = [i for i in ids if i not in found]
    if missing:
        raise AcceptError(f"no verdict for {', '.join(missing)}")
    return [found[i] for i in ids]


def _check(cfg: RunnerConfig, verdict: dict) -> dict:
    """Hold a "met" to its evidence: every cited test must exist and pass."""
    if verdict["verdict"] != "met":
        return verdict
    if not verdict["tests"]:
        return dict(verdict, verdict="unmet", reason="the acceptor cited no test")
    for cited in verdict["tests"]:
        path = cited.split("::", 1)[0]
        try:
            full = resolve_test_path(cfg, path)
        except BuildError as e:
            return dict(verdict, verdict="unmet", reason=f"cited test {cited}: {e}")
        if not full.is_file():
            return dict(verdict, verdict="unmet", reason=f"cited test {path} does not exist")
        try:
            passed, _ = run_tests(cfg, path)
        except BuildError as e:
            return dict(verdict, verdict="unmet", reason=f"cited test {path} did not run: {e}")
        if not passed:
            return dict(verdict, verdict="unmet", reason=f"cited test {path} fails")
    return verdict


# --- Dev checks -----------------------------------------------------------------

PASS_LINE = "ACCEPT-PASS"
FAIL_PREFIX = "ACCEPT-FAIL"

DEV_CHECK_PROMPT = """\
You are acceptance.tester in an automated delivery pipeline working on this repository.
You have read-only access.

GitHub issue #{number}: {title}

{body}

This acceptance criterion can only be observed in the deployed Dev environment:
{criterion_id}: {criterion}

The change meant to satisfy it is built on this branch but NOT deployed yet.
Its diff against the base:
{diff}

Write ONE executable check for this criterion. The harness will run:
  {command}
with the check saved as a `{ext}` file. Dev is at: {dev_url}

How checks are written for this repository:
{guide}

HARD RULES:
- The check exercises the real Dev environment and decides the criterion itself.
- The LAST line it prints must be exactly `ACCEPT-PASS` when the criterion holds,
  or `ACCEPT-FAIL <reason>` when it does not. Nothing may follow that line.
- Run now, before the change is deployed, it MUST print ACCEPT-FAIL. A check
  that passes now proves nothing about this change and will be rejected.
- Do not modify any file.

{rules}
Reply with ONLY this JSON (no prose):
{{"content": "<the complete check file>"}}
{feedback}"""


@dataclass
class CheckResult:
    verdict: str | None  # "pass", "fail", or None when the check gave no verdict
    reason: str
    output: str


def harness_problem(cfg: RunnerConfig, criteria: list[dict]) -> str | None:
    """Why the Dev criteria cannot be checked with this configuration, if they cannot."""
    dev = [c["id"] for c in criteria if c["where"] == "dev"]
    if not dev:
        return None
    settings = cfg.deploy_settings
    if not settings.dev_check_cmd.strip():
        return (
            f"criteria {', '.join(dev)} can only be checked in Dev, but [deploy] "
            "dev_check_cmd is not set"
        )
    if "{check_path}" not in settings.dev_check_cmd:
        return "[deploy] dev_check_cmd must contain {check_path}"
    if "{dev_url}" in settings.dev_check_cmd and not settings.dev_url.strip():
        return "[deploy] dev_check_cmd uses {dev_url}, but dev_url is not set"
    return None


def check_dir(store: TicketStore) -> Path:
    return store.state_dir / "acceptance" / f"issue-{store.issue_ref}"


def run_check(cfg: RunnerConfig, check_path: Path, criterion_id: str) -> CheckResult:
    """Run one Dev check. Only the last line of its output is its verdict."""
    settings = cfg.deploy_settings
    command = settings.dev_check_cmd.format(
        check_path=shlex.quote(str(check_path)),
        dev_url=shlex.quote(settings.dev_url),
        criterion=shlex.quote(criterion_id),
    )
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=str(check_path.parent),
            capture_output=True,
            text=True,
            timeout=settings.dev_check_timeout_sec,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(None, f"timed out after {settings.dev_check_timeout_sec}s", "")
    except (OSError, ValueError) as e:
        return CheckResult(None, f"could not run `{command}`: {e}", "")
    output = (result.stdout or "") + (result.stderr or "")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last == PASS_LINE:
        return CheckResult("pass", "", output)
    if last.startswith(FAIL_PREFIX):
        return CheckResult("fail", last[len(FAIL_PREFIX) :].strip(), output)
    return CheckResult(
        None,
        f"the last line printed was {last!r}, not {PASS_LINE} or {FAIL_PREFIX} "
        f"(exit {result.returncode})",
        output,
    )


def write_red_check(
    client, cfg: RunnerConfig, issue: dict, store: TicketStore, criterion: dict
) -> dict:
    """Write a Dev check that fails against today's Dev, freeze it, and describe it."""
    settings = cfg.deploy_settings
    folder = check_dir(store)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{criterion['id']}{settings.dev_check_ext}"
    args = {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "criterion_id": criterion["id"],
        "criterion": criterion["text"],
        "diff": devops.diff_since(cfg.repo_dir, store.initial_head),
        "command": settings.dev_check_cmd,
        "ext": settings.dev_check_ext,
        "dev_url": settings.dev_url or "(see the command)",
        "guide": settings.dev_check_guide.strip() or "(no guide: follow the command above)",
        "rules": workspace_rules(cfg),
    }
    extra = ""
    last_error = "no attempt"
    for _ in range(cfg.tester_retries + 1):
        reply = client.run(
            DEV_CHECK_PROMPT.format(**args, feedback=extra),
            role="acceptance.tester",
            read_only=True,
            session_name=f"acceptance-{criterion['id']}",
        )
        try:
            content = extract_json_object(reply).get("content")
        except (JsonExtractError, TypeError) as e:
            content, last_error = None, f"the reply was not the required JSON: {e}"
        if content is not None and (not isinstance(content, str) or not content.strip()):
            content, last_error = None, '"content" must be the complete check file'
        if content is not None:
            path.write_text(content)
            result = run_check(cfg, path, criterion["id"])
            if result.verdict == "fail":
                return {
                    "check_path": str(path),
                    "check_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "red_output": result.reason,
                }
            if result.verdict == "pass":
                last_error = (
                    "the check printed ACCEPT-PASS against Dev before the change is "
                    "deployed, so it does not test this change. Output:\n" + result.output[-1500:]
                )
            else:
                last_error = (
                    f"the check gave no verdict: {result.reason}. Output:\n"
                    + (result.output[-1500:])
                )
        extra = f"\nYOUR PREVIOUS CHECK WAS REJECTED (fix this):\n{last_error}"
    path.unlink(missing_ok=True)
    raise AcceptError(f"no failing-first Dev check for {criterion['id']}: {last_error}")


def frozen_check(criterion: dict) -> Path:
    """The saved check, proven unchanged since it was accepted."""
    path = Path(criterion.get("check_path") or "")
    if not path.is_file():
        raise AcceptError(f"the Dev check for {criterion['id']} is missing at {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != criterion.get("check_hash"):
        raise AcceptError(f"the Dev check for {criterion['id']} changed after it was accepted")
    return path
