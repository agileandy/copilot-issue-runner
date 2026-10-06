"""Acceptance criteria: read them from an issue body and tick them off again.

An issue states its criteria under a heading such as ``## Acceptance criteria``
or a plain ``Acceptance criteria:`` line, as a list of items, often checkboxes.
Each top-level item becomes one criterion with a stable ID (AC1, AC2, ...), so
the plan, the tests and the Dev checks can all name the one they prove.
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*acceptance\s+criteria\b[^\n]*$", re.IGNORECASE
)
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+(?:\[(?P<box>[ xX])\]\s+)?(?P<text>\S.*)$")


@dataclass
class Criterion:
    id: str
    text: str
    line: int  # index of the item's line in body.splitlines()
    checked: bool
    checkbox: bool


def parse(body: str | None) -> list[Criterion]:
    """The criteria in the first acceptance-criteria section, in order."""
    lines = (body or "").splitlines()
    start = next((i for i, line in enumerate(lines) if _HEADING.match(line)), None)
    if start is None:
        return []
    found: list[Criterion] = []
    list_indent = None  # the first item's indent; deeper items nest under the one above
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _MD_HEADING.match(line):
            break
        if not line.strip():
            continue
        item = _ITEM.match(line)
        indent = len(item.group("indent").expandtabs(4)) if item else None
        if item and list_indent is None and indent <= 3:
            list_indent = indent
        if item and list_indent is not None and indent <= list_indent:
            box = item.group("box")
            found.append(
                Criterion(
                    id=f"AC{len(found) + 1}",
                    text=item.group("text").strip(),
                    line=i,
                    checked=box in ("x", "X"),
                    checkbox=box is not None,
                )
            )
        elif found and line[:1].isspace():
            # a wrapped line or nested item still belongs to the criterion above
            found[-1].text += " " + line.strip()
        elif found:
            break  # the list ended; what follows is ordinary text
    return found


def tick(body: str, criteria: list[Criterion], passed: set[str]) -> str:
    """Mark the passed checkbox criteria as done, leaving every other line alone.

    The criteria must come from parsing this same body: a line that no longer
    reads as the expected unchecked item is left untouched rather than guessed at.
    """
    lines = body.splitlines(keepends=True)
    for criterion in criteria:
        if criterion.id not in passed or not criterion.checkbox or criterion.checked:
            continue
        if criterion.line >= len(lines):
            continue
        line = lines[criterion.line]
        updated = re.sub(r"\[ \]", "[x]", line, count=1)
        if updated != line and _ITEM.match(updated.rstrip("\r\n")):
            lines[criterion.line] = updated
    return "".join(lines)
