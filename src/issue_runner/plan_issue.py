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


def split_assertion(text: str) -> tuple[str, str] | None:
    """Split a task at the first ' — assert:' outside inline `code`, which only quotes it."""
    token = ASSERT_SEP.rstrip()
    in_code = False
    for i, char in enumerate(text):
        if char == "`":
            in_code = not in_code
        elif not in_code and text.startswith(token, i):
            return text[:i].strip(), text[i + len(token) :].strip()
    return None


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
    gates_ticked: set = field(default_factory=set)  # (phase or None, command) already ticked


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
    command = found.group(1).strip()
    if not command:
        raise PlanIssueError(f"gate checkbox has an empty `command`: {line!r}")
    return command


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
    gates_ticked: set = set()
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
            if task.checked:
                gates_ticked.add((phase, phase_gates[phase][-1]))
            continue
        parts = split_assertion(task.text)
        if parts is None or not parts[1].strip(" `"):
            raise PlanIssueError(f"task has no '{ASSERT_SEP.strip()}' test assertion: {line!r}")
        action, assertion = parts
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

    final_gates = []
    for task in map(split_line, sections.get("Validation Commands", [])):
        if task is not None:
            final_gates.append(_command(task.text))
            if task.checked:
                gates_ticked.add((None, final_gates[-1]))
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
        gates_ticked=gates_ticked,
    )


def _ticket_line(task: TaskLine, ticket) -> TaskLine:
    reason = None
    if ticket.status == "blocked":
        reason = (ticket.blocked_reason or "blocked").strip().splitlines()[0][:160]
    state = {"in_progress": "wip", "blocked": "failed"}.get(ticket.status, "")
    return TaskLine(ticket.status == "done", state, task.text, ticket.github_issue, reason)


def _gate_line(task: TaskLine, status: str | None) -> TaskLine:
    if status == "fail":
        return TaskLine(False, "failed", task.text, None, "gate command failed")
    return TaskLine(status == "pass", "", task.text, None, None)


def _phase_status(tickets: list, gates: list[str | None]) -> str:
    if any(t.status == "blocked" for t in tickets) or "fail" in gates:
        return "failed"
    if tickets and all(t.status == "done" for t in tickets) and all(g != "pending" for g in gates):
        return "done"
    if any(t.status in ("in_progress", "done") for t in tickets) or "pass" in gates:
        return "wip"
    return "idle"


def rewrite(body: str, tickets: list, gates: dict | None = None) -> str:
    """Write the run's state into the plan's runner-owned markers, and nothing else.

    `gates` maps (phase number, command) for a phase gate, or (None, command)
    for a final gate, to "pass", "fail", "skipped" or "pending". Only task
    lines, gate lines and phase headings change, so text a person wrote in the
    plan is left byte for byte as it was.
    """
    gates = gates or {}
    crlf = "\r\n" in body
    lines = body.replace("\r\n", "\n").split("\n")
    by_key = {(t.plan_phase, t.plan_task): t for t in tickets if t.plan_task}
    section = None
    phase = None
    in_gate = False
    in_fence = False
    headings: dict[int, int] = {}
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        h2 = _H2.match(line)
        if h2:
            section, phase, in_gate = h2.group(1), None, False
            continue
        task = split_line(line)
        if section == "Implementation Phases":
            match = _PHASE.match(line)
            if match:
                phase, in_gate = int(match.group(1)), False
                headings[phase] = i
                continue
            heading = _TASK_HEADING.match(line)
            if heading:
                in_gate = "testing strategy" in heading.group(1).lower()
                continue
            if task is None or phase is None:
                continue
            if in_gate:
                command = _COMMAND.search(task.text)
                if command:
                    status = gates.get((phase, command.group(1).strip()))
                    lines[i] = render_line(_gate_line(task, status))
            elif (phase, task.text) in by_key:
                lines[i] = render_line(_ticket_line(task, by_key[(phase, task.text)]))
        elif section == "Validation Commands" and task is not None:
            command = _COMMAND.search(task.text)
            if command:
                lines[i] = render_line(_gate_line(task, gates.get((None, command.group(1).strip()))))
    for number, index in headings.items():
        mine = [t for t in tickets if t.plan_phase == number]
        statuses = [s for (p, _), s in gates.items() if p == number]
        status = _phase_status(mine, statuses)
        line = re.sub(r" · `(idle|wip|done|failed)`\s*$", "", lines[index])
        lines[index] = f"{line} · `{status}`"
    text = "\n".join(lines)
    return text.replace("\n", "\r\n") if crlf else text


class PlanBodyMirror:
    """Mirror run state into the plan issue's body: the GitHub side of rewrite()."""

    def __init__(self, repo: str, number: int, fetch=None, edit=None):
        from . import github_io

        self.repo = repo
        self.number = number
        self._fetch = fetch or (lambda: github_io.fetch_issue(str(number), repo=repo)["body"])
        self._edit = edit or (lambda body: github_io.edit_issue_body(repo, number, body))

    def sync(self, tickets: list, gates: dict | None = None, attempts: int = 3) -> bool:
        """Rewrite the markers against the latest body; False when nothing changed.

        GitHub has no conditional write for an issue body, so a person's edit can
        still land between the last read and the write. The body is read again
        just before writing and after it, and the rewrite is retried when it moved,
        which leaves that window as small as the API allows.
        """
        for _ in range(attempts):
            body = self._fetch() or ""
            updated = rewrite(body, tickets, gates)
            if updated == body:
                return False
            if (self._fetch() or "") != body:
                continue  # edited while we were rewriting: start again from the new body
            self._edit(updated)
            if (self._fetch() or "") == updated:
                return True
        raise PlanIssueError(
            f"#{self.number} kept changing while the runner updated its checkboxes; "
            "they will be rewritten at the next update"
        )
