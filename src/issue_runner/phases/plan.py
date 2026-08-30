"""Plan phase: one read-only Copilot call turns issue + codebase into atomic tickets."""

from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json
from ..tickets import Ticket


class PlanError(RuntimeError):
    pass


PLAN_PROMPT = """\
You are the planner in an automated TDD pipeline working on this repository.

GitHub issue #{number}: {title}

{body}

Explore the codebase (read-only) and produce a detailed, atomic implementation
plan for this issue as an ordered list of sub-task tickets.

RULES for tickets:
- Each ticket is ONE atomic BEHAVIOUR change: the production code AND the test
  that proves it belong to the SAME ticket. NEVER create test-only, code-only,
  or refactor-only tickets — a ticket whose behaviour already exists cannot be
  test-driven and will be rejected.
- Each ticket is small (roughly <=50 lines of production code).
- Each ticket has exactly ONE logical test assertion — the single observable
  behaviour that proves the ticket done. Be concrete: name real functions,
  inputs, and expected outputs from THIS codebase.
- Order tickets so earlier ones never depend on later ones.
- Do NOT write any code or modify any file. Planning only.

Reply with ONLY this JSON (no prose before or after):
{{
  "summary": "<one-line plan summary>",
  "tickets": [
    {{
      "title": "<short imperative title>",
      "description": "<what to change and where>",
      "test_assertion": "<the single assertion, concrete>",
      "files_hint": ["<likely files>"]
    }}
  ]
}}
{feedback}"""

REQUIRED_FIELDS = ("title", "description", "test_assertion")


def plan_step(client, cfg: RunnerConfig, issue: dict) -> tuple[str, list[Ticket]]:
    extra = ""
    last_error = "no attempt"
    for _ in range(2):
        prompt = PLAN_PROMPT.format(
            number=issue["number"], title=issue["title"], body=issue["body"], feedback=extra
        )
        reply = client.run(prompt, role="planner", read_only=True, session_name="planner")
        try:
            data = extract_json(reply)
            return data.get("summary", ""), _validate(data)
        except (JsonExtractError, PlanError, TypeError) as e:
            last_error = str(e)
            extra = f"\nYOUR PREVIOUS REPLY WAS INVALID (fix this):\n{last_error}"
    raise PlanError(f"planner failed: {last_error}")


def _validate(data: dict) -> list[Ticket]:
    raw = data.get("tickets")
    if not isinstance(raw, list) or not raw:
        raise PlanError("plan contains no tickets")
    tickets = []
    for i, item in enumerate(raw, start=1):
        for field in REQUIRED_FIELDS:
            if not item.get(field):
                raise PlanError(f"ticket {i} is missing required field {field!r}")
        tickets.append(
            Ticket(
                id=i,
                title=item["title"],
                description=item["description"],
                test_assertion=item["test_assertion"],
                files_hint=list(item.get("files_hint", [])),
                depends_on=list(item.get("depends_on", [])),
            )
        )
    return tickets
