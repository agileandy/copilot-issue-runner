"""Acceptance: does the finished change meet the issue's acceptance criteria?

Before a PR opens, a read-only acceptor agent judges every criterion that is
proven by tests. Its verdict is advisory until the harness checks it: a "met"
counts only when it cites at least one test file that exists in the worktree
and passes when the harness runs it. Anything else is "unmet".
"""

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
