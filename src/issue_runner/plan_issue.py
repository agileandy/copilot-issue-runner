"""Read a plan-gh v1 issue body as a ready-made plan.

With --plan-from-issue the runner makes no planner call: every task checkbox
in the plan's Implementation Phases becomes one ticket, and the Testing
Strategy and Validation Commands checkboxes become phase and final gates.
The accepted format is documented in README.md ("Plans from a plan issue").
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from .phases.plan import _validate
from .tickets import Ticket

MARKER = "<!-- plan-gh:plan v1 -->"
ASSERT_SEP = " — assert: "
FAILED_SEP = " — failed: "

_H2 = re.compile(r"^## (.+?)\s*$")
_PHASE = re.compile(r"^### Phase (\d+): (.+?)(?: · `(idle|wip|done|failed)`)?\s*$")
_TASK_HEADING = re.compile(r"^#### (.+?)\s*$")
_CHECKBOX = re.compile(r"^- \[( |x|X)\] (.*)$")
_SUB_ISSUE = re.compile(r" · #(\d+)$")
_COMMAND = re.compile(r"`([^`\n]+)`")


class PlanIssueError(ValueError):
    pass


@dataclass(frozen=True)
class TaskLine:
    """One checkbox line, split into the runner-owned markers and the plan's text."""

    checked: bool
    state: str  # "", "wip" or "failed"
    text: str
    sub_issue: int | None
    reason: str | None


def split_line(line: str) -> TaskLine | None:
    match = _CHECKBOX.match(line)
    if not match:
        return None
    rest = match.group(2)
    reason = None
    if FAILED_SEP in rest:
        rest, reason = rest.split(FAILED_SEP, 1)
    sub = _SUB_ISSUE.search(rest)
    sub_issue = int(sub.group(1)) if sub else None
    if sub:
        rest = rest[: sub.start()]
    state = ""
    for word in ("wip", "failed"):
        if rest.startswith(word + " "):
            state, rest = word, rest[len(word) + 1 :]
            break
    return TaskLine(match.group(1) != " ", state, rest.strip(), sub_issue, reason)


def render_line(task: TaskLine) -> str:
    box = "x" if task.checked else " "
    state = f"{task.state} " if task.state else ""
    sub = f" · #{task.sub_issue}" if task.sub_issue else ""
    reason = f"{FAILED_SEP}{task.reason}" if task.reason else ""
    return f"- [{box}] {state}{task.text}{sub}{reason}"


@dataclass
class ParsedPlan:
    summary: str
    tickets: list[Ticket]
    phase_names: dict[int, str] = field(default_factory=dict)
    phase_gates: dict[int, list[str]] = field(default_factory=dict)
    final_gates: list[str] = field(default_factory=list)
    fingerprint: str = ""


def _sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    in_fence = False
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else _H2.match(line)
        if heading:
            current = heading.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def _command(line: str) -> str:
    found = _COMMAND.search(line)
    if not found:
        raise PlanIssueError(f"gate checkbox has no `command`: {line!r}")
    return found.group(1).strip()


def parse(body: str, title: str = "") -> ParsedPlan:
    """Turn a plan-gh v1 body into tickets and gates, or raise PlanIssueError.

    Tickets also pass the planner's own validation (phases.plan._validate),
    which raises PlanError for a ticket the build loop could not prove.
    """
    if MARKER not in (body or ""):
        raise PlanIssueError(f"the issue is not a plan-gh plan: its body has no {MARKER} marker")
    sections = _sections(body)
    if "Implementation Phases" not in sections:
        raise PlanIssueError("the plan has no '## Implementation Phases' section")

    raw: list[dict] = []
    keys: list[tuple[int, str, bool]] = []
    phase_names: dict[int, str] = {}
    phase_gates: dict[int, list[str]] = {}
    phase = None
    phase_intro: list[str] = []
    heading = ""
    in_gate = False
    in_fence = False
    for line in sections["Implementation Phases"]:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        match = _PHASE.match(line)
        if match:
            phase = int(match.group(1))
            if phase in phase_names:
                raise PlanIssueError(f"phase {phase} appears twice")
            phase_names[phase] = match.group(2).strip()
            phase_gates[phase] = []
            phase_intro, heading, in_gate = [], "", False
            continue
        if phase is None:
            continue
        task_heading = _TASK_HEADING.match(line)
        if task_heading:
            heading = task_heading.group(1)
            in_gate = "testing strategy" in heading.lower()
            continue
        task = split_line(line)
        if task is None:
            if not heading and line.strip() and not line.startswith(("_", "<!--")):
                phase_intro.append(line.strip())
            continue
        if in_gate:
            phase_gates[phase].append(_command(task.text))
            continue
        if ASSERT_SEP not in task.text:
            raise PlanIssueError(f"task has no '{ASSERT_SEP.strip()}' test assertion: {line!r}")
        action, assertion = task.text.split(ASSERT_SEP, 1)
        if any(k[:2] == (phase, task.text) for k in keys):
            raise PlanIssueError(f"phase {phase} lists the same task twice: {line!r}")
        description = f"Plan phase {phase}: {phase_names[phase]}. Task group: {heading or 'tasks'}."
        if phase_intro:
            description += "\n\n" + " ".join(phase_intro)
        raw.append(
            {
                "title": action.strip(),
                "description": description,
                "test_assertion": assertion.strip(),
                "files_hint": [],
            }
        )
        keys.append((phase, task.text, task.checked))
    if not raw:
        raise PlanIssueError("the plan has no task checkboxes under Implementation Phases")

    tickets = _validate({"tickets": raw})
    for ticket, (phase_no, text, checked) in zip(tickets, keys):
        ticket.plan_phase = phase_no
        ticket.plan_task = text
        if checked:
            ticket.status = "done"

    final_gates = [
        _command(task.text)
        for task in map(split_line, sections.get("Validation Commands", []))
        if task is not None
    ]
    shape = {
        "tasks": [[p, t] for p, t, _ in keys],
        "phase_gates": {str(k): v for k, v in phase_gates.items()},
        "final_gates": final_gates,
    }
    fingerprint = hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()
    summary = title.removeprefix("Plan: ").strip() or "plan-gh plan"
    return ParsedPlan(
        summary=f"from plan issue: {summary}",
        tickets=tickets,
        phase_names=phase_names,
        phase_gates=phase_gates,
        final_gates=final_gates,
        fingerprint=fingerprint,
    )
