"""Verify phase: a read-only Copilot call judges test robustness and routes the loop."""

from dataclasses import dataclass

from ..config import RunnerConfig
from ..journal import Journal
from ..jsonx import JsonExtractError, extract_json_object
from ..tickets import Ticket

VERDICTS = ("pass", "refine_test", "rework_code")


class VerifyError(RuntimeError):
    pass


@dataclass
class Verdict:
    verdict: str
    reasons: list[str]
    test_feedback: str = ""
    code_feedback: str = ""


VERIFY_PROMPT = """\
You are the verifier in an automated TDD pipeline working on this repository.
You have read-only access, but you MAY run the test suite via shell commands.

Sub-task under review: {title}
Description: {description}
Required single logical assertion: {test_assertion}
The test lives at: {test_path}
The implementation is in the current working tree (inspect `git status` is
denied — read the files directly).

Earlier rounds of this sub-task, if any, are recorded on its own issue:
{thread}
Read that thread before judging, so you do not repeat feedback that has already
been given or re-reject something you previously accepted.

Judge, in this order:
1. Is the test ROBUST? It must genuinely exercise the asserted behaviour,
   cover the relevant edge cases for this ticket, use exactly one logical
   assertion, and not be a stub or tautology.
2. Does the implementation actually pass the test? Run it: `{test_cmd}`

Choose exactly one verdict:
- "refine_test"  -> the test is weak, a stub, or misses edge cases (explain in test_feedback)
- "rework_code"  -> the test is robust but the implementation is wrong or fails (explain in code_feedback)
- "pass"         -> the test is robust AND the implementation passes it

Reply with ONLY this JSON (no prose):
{{
  "verdict": "pass" | "refine_test" | "rework_code",
  "reasons": ["<short reasons>"],
  "test_feedback": "<what the tester must improve, if refine_test>",
  "code_feedback": "<what the coder must fix, if rework_code>"
}}
{feedback}"""


def verify_step(
    client,
    cfg: RunnerConfig,
    ticket: Ticket,
    test_path: str,
    journal: Journal | None = None,
) -> Verdict:
    journal = journal or Journal()
    thread = journal.thread(ticket)
    thread_line = f"  {thread}" if thread else "  (no issue thread for this run)"
    extra = ""
    last_error = "no attempt"
    for _ in range(2):
        prompt = VERIFY_PROMPT.format(
            title=ticket.title,
            description=ticket.description,
            test_assertion=ticket.test_assertion,
            test_path=test_path,
            test_cmd=cfg.test_cmd.format(test_path=test_path),
            thread=thread_line,
            feedback=extra,
        )
        reply = client.run(
            prompt, role="verifier", read_only=True, session_name=f"verifier-t{ticket.id}"
        )
        try:
            data = extract_json_object(reply)
            verdict = data.get("verdict")
            if verdict not in VERDICTS:
                raise VerifyError(f"invalid verdict {verdict!r}; must be one of {VERDICTS}")
            return Verdict(
                verdict=verdict,
                reasons=list(data.get("reasons", [])),
                test_feedback=str(data.get("test_feedback", "")),
                code_feedback=str(data.get("code_feedback", "")),
            )
        except (JsonExtractError, VerifyError, TypeError) as e:
            last_error = str(e)
            extra = f"\nYOUR PREVIOUS REPLY WAS INVALID (fix this):\n{last_error}"
    raise VerifyError(f"verifier failed: {last_error}")
