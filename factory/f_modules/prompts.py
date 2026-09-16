"""Render prompt placeholders, and save the audit copy.

§5.6 allows **exactly three** substitutions, and rendering is literal string replacement. There is
no template engine and no logic in prompts, because logic in prompts is how prompts stop being
reviewable — a conditional in a prompt is behaviour that never appears in a diff anyone reads.

The two-file split (§11.1): ``system.md`` is **identity** (Purpose, Instructions) and changes when
the agent's role changes; ``user.md`` is **task shape** (Variables, Task, Report) and changes when
the call's contract changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from f_modules.data_types import EnvelopeBase

PLACEHOLDERS = ("{{prompt}}", "{{previous_envelope}}", "{{context_handoff_dir}}")
"""The only three. Adding a fourth means editing this module, which is the point."""

NO_PREVIOUS = "(none)"


def render(
    template: str,
    prompt: str,
    previous: EnvelopeBase | None = None,
    context_handoff_dir: str = "",
) -> str:
    """Substitute the three placeholders, literally."""
    previous_json = (
        previous.model_dump_json(indent=2) if previous is not None else NO_PREVIOUS
    )
    rendered = template.replace("{{prompt}}", prompt)
    rendered = rendered.replace("{{previous_envelope}}", previous_json)
    return rendered.replace("{{context_handoff_dir}}", context_handoff_dir)


def unknown_placeholders(template: str) -> list[str]:
    """Any ``{{...}}`` the renderer will not substitute.

    A prompt asking for a placeholder that does not exist renders it literally into the agent's
    context, where it reads as an instruction nobody wrote. Cheap to detect, invisible otherwise.
    """
    import re

    found = re.findall(r"\{\{[^}]*\}\}", template)
    return sorted({token for token in found if token not in PLACEHOLDERS})


def save_audit_copy(session_dir: Path, agent: str, system: str, user: str) -> dict[str, str]:
    """Write the exact prompts to disk **before** execution (§5.8).

    Saved before rather than after, so a run that hangs or is killed still leaves behind exactly what
    the agent was asked. A prompt reconstructed afterwards is a reconstruction, not evidence.
    """
    target = Path(session_dir) / agent / "prompts"
    target.mkdir(parents=True, exist_ok=True)
    (target / "system.md").write_text(system)
    (target / "user.md").write_text(user)
    return {"system": str(target / "system.md"), "user": str(target / "user.md")}


def report_example(user_template: str) -> dict | None:
    """Extract the JSON example under ``## Report`` — leg (b) of §5.5's synced triad.

    Exposed so the triad can be *checked* rather than merely asserted: the example must match the
    type definition field for field, or every call pays a correction round-trip to be told a shape
    the parser was always going to reject.
    """
    marker = user_template.find("```json")
    if marker == -1:
        return None
    start = user_template.find("\n", marker) + 1
    end = user_template.find("```", start)
    if end == -1:
        return None
    try:
        return json.loads(user_template[start:end])
    except json.JSONDecodeError:
        return None
