"""Plan phase: one read-only Copilot call turns issue + codebase into atomic tickets."""

from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json_object
from ..tickets import Ticket


class PlanError(RuntimeError):
    pass


TICKET_RULES = """\
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
- Do NOT write any code or modify any file. Planning only."""

PLAN_PROMPT = """\
You are the planner in an automated TDD pipeline working on this repository.

GitHub issue #{number}: {title}

{body}

Explore the codebase (read-only) and produce a detailed, atomic implementation
plan for this issue as an ordered list of sub-task tickets.

{rules}
{criteria}
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

CRITERIA_BLOCK = """
Acceptance criteria. The plan MUST cover every one of them:
{lines}

For each criterion decide where it can be proven:
- "tests": an automated test in this repository can prove it. Name its id in
  the "criteria" list of every ticket whose test proves it.
- "dev": it can only be observed in the deployed Dev environment (a real
  browser, real data or real infrastructure). It is checked after deployment.
  Tickets that implement it may still name it.

Add to the JSON reply a top-level
  "criteria": [{{"id": "AC1", "where": "tests"}}, ...]   (exactly one entry per criterion)
and on each ticket
  "criteria": ["AC1", ...]
"""

EXTRA_PROMPT = """\
You are the planner in an automated TDD pipeline working on this repository.

GitHub issue #{number}: {title}

{body}

The tickets below are built and all their tests pass, but a reviewer judged
these acceptance criteria NOT yet met:
{unmet}

Tickets already built:
{existing}

Explore the codebase (read-only) and plan ADDITIONAL tickets that make every
listed criterion met.

{rules}
- Number the new tickets from {first_id}. "depends_on" may name built tickets.
- Name the criteria each new ticket proves in its "criteria" list. Every listed
  criterion must be named by at least one new ticket.

Reply with ONLY this JSON (no prose before or after):
{{
  "summary": "<one-line summary of the additional tickets>",
  "tickets": [
    {{
      "title": "<short imperative title>",
      "description": "<what to change and where>",
      "test_assertion": "<the single assertion, concrete>",
      "files_hint": ["<likely files>"],
      "criteria": ["AC1"]
    }}
  ]
}}
{feedback}"""

REQUIRED_FIELDS = ("title", "description", "test_assertion")
WHERE = ("tests", "dev")


def build_plan_prompt(issue: dict, criteria=None, feedback: str = "") -> str:
    block = ""
    if criteria:
        block = CRITERIA_BLOCK.format(lines="\n".join(f"{c.id}: {c.text}" for c in criteria))
    return PLAN_PROMPT.format(
        number=issue["number"],
        title=issue["title"],
        body=issue["body"],
        rules=TICKET_RULES,
        criteria=block,
        feedback=feedback,
    )


def plan_step(client, cfg: RunnerConfig, issue: dict) -> tuple[str, list[Ticket]]:
    summary, tickets, _ = _ask(
        client,
        lambda extra: build_plan_prompt(issue, feedback=extra),
        lambda data: (_validate(data), {}),
    )
    return summary, tickets


def plan_with_criteria(
    client, cfg: RunnerConfig, issue: dict, criteria
) -> tuple[str, list[Ticket], dict[str, str]]:
    """Plan tickets that cover every acceptance criterion; also say where each is proven."""
    ids = [c.id for c in criteria]

    def validate(data):
        tickets = _validate(data, criteria_ids=ids)
        return tickets, _validate_where(data, ids, tickets)

    return _ask(client, lambda extra: build_plan_prompt(issue, criteria, extra), validate)


def plan_extra(
    client, cfg: RunnerConfig, issue: dict, unmet: list[dict], existing: list[Ticket], all_ids
) -> tuple[str, list[Ticket]]:
    """Plan additional tickets for the criteria the acceptor judged unmet."""
    first_id = max((t.id for t in existing), default=0) + 1
    unmet_ids = [u["id"] for u in unmet]

    def prompt(extra):
        return EXTRA_PROMPT.format(
            number=issue["number"],
            title=issue["title"],
            body=issue["body"],
            unmet="\n".join(f"{u['id']}: {u['text']} — {u['reason']}" for u in unmet),
            existing="\n".join(
                f"{t.id}. {t.title} (criteria: {', '.join(t.criteria) or 'none'})" for t in existing
            ),
            rules=TICKET_RULES,
            first_id=first_id,
            feedback=extra,
        )

    def validate(data):
        tickets = _validate(
            data, criteria_ids=all_ids, first_id=first_id, existing_ids=[t.id for t in existing]
        )
        named = {c for t in tickets for c in t.criteria}
        missing = [i for i in unmet_ids if i not in named]
        if missing:
            raise PlanError(f"no new ticket names criteria {', '.join(missing)}")
        return tickets, {}

    summary, tickets, _ = _ask(client, prompt, validate)
    return summary, tickets


def _ask(client, prompt_for, validate):
    extra = ""
    last_error = "no attempt"
    for _ in range(2):
        reply = client.run(
            prompt_for(extra), role="planner", read_only=True, session_name="planner"
        )
        try:
            data = extract_json_object(reply)
            tickets, where = validate(data)
            return data.get("summary", ""), tickets, where
        except (JsonExtractError, PlanError, TypeError) as e:
            last_error = str(e)
            extra = f"\nYOUR PREVIOUS REPLY WAS INVALID (fix this):\n{last_error}"
    raise PlanError(f"planner failed: {last_error}")


def _validate(data: dict, criteria_ids=None, first_id: int = 1, existing_ids=()) -> list[Ticket]:
    raw = data.get("tickets")
    if not isinstance(raw, list) or not raw:
        raise PlanError("plan contains no tickets")
    known = set(existing_ids) | set(range(first_id, first_id + len(raw)))
    tickets = []
    for i, item in enumerate(raw, start=first_id):
        for field in REQUIRED_FIELDS:
            if not item.get(field):
                raise PlanError(f"ticket {i} is missing required field {field!r}")
        depends_on = list(item.get("depends_on", []))
        for dep in depends_on:
            if dep not in known or dep == i:
                raise PlanError(f"ticket {i} depends on {dep!r}, which is not another ticket")
        named = list(item.get("criteria", [])) if criteria_ids is not None else []
        unknown = [c for c in named if c not in criteria_ids]
        if unknown:
            raise PlanError(f"ticket {i} names unknown criteria {', '.join(map(str, unknown))}")
        tickets.append(
            Ticket(
                id=i,
                title=item["title"],
                description=item["description"],
                test_assertion=item["test_assertion"],
                files_hint=list(item.get("files_hint", [])),
                depends_on=depends_on,
                criteria=named,
            )
        )
    return tickets


def _validate_where(data: dict, ids: list[str], tickets: list[Ticket]) -> dict[str, str]:
    raw = data.get("criteria")
    if not isinstance(raw, list):
        raise PlanError('the reply has no top-level "criteria" list')
    where: dict[str, str] = {}
    for entry in raw:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if cid not in ids:
            raise PlanError(f"unknown criterion {cid!r} in the criteria list")
        if cid in where:
            raise PlanError(f"criterion {cid} is listed twice")
        if entry.get("where") not in WHERE:
            raise PlanError(f'criterion {cid} needs "where": "tests" or "dev"')
        where[cid] = entry["where"]
    missing = [i for i in ids if i not in where]
    if missing:
        raise PlanError(f"the plan does not cover criteria {', '.join(missing)}")
    named = {c for t in tickets for c in t.criteria}
    untested = [i for i in ids if where[i] == "tests" and i not in named]
    if untested:
        raise PlanError(
            f"criteria {', '.join(untested)} are proven by tests but no ticket names them"
        )
    return where
