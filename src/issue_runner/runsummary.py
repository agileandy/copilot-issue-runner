"""Collect what a run actually produced and why it stopped short.

Only tickets carrying a ``commit_sha`` count as artefacts. A ticket without one
produced no durable artefact, however much work was attempted against it.
"""

import re

from .tickets import TicketStore

# Lines in a block reason that name the underlying failure. The reason's first
# line says what the runner saw; these say why, e.g. a missing module.
_CAUSE_PATTERNS = (
    re.compile(r"^E\s+(\S.*)$"),  # pytest's failure lines
    re.compile(r"^\s*([\w.]*(?:Error|Exception): .+)$"),
    re.compile(r"^(HINT: .+)$"),
)
_MAX_CAUSES = 4

# Where in a ticket's pipeline it blocked, in words a reader can act on.
_STAGES = {
    "tester": "writing the ticket's test",
    "refine_test": "revising the ticket's test",
    "coder": "writing the code that passes the ticket's test",
    "verifier": "reviewing the change",
    "regression": "full test-suite check, after the ticket's own test passed review",
    "commit": "committing the approved change",
    "dependencies": "waiting on tickets that cannot finish",
}

# Per-ticket details already shown as blocked tickets, commits or the plan.
_TICKET_DETAIL = re.compile(r"^ticket \d+ (?:BLOCKED:|done @|\[)")


def artefacts(store: TicketStore) -> dict:
    committed = [t for t in store.tickets if t.commit_sha]
    files: set[str] = set()
    commits = []
    for t in committed:
        files.update(t.changed_files)
        commits.append(
            {
                "sha": t.commit_sha,
                "ticket_id": t.id,
                "title": t.title,
                "files": list(t.changed_files),
            }
        )
    return {"files": sorted(files), "commits": commits, "pr_url": store.pr_url}


def causes(reason: str) -> list[str]:
    """The distinct error lines inside a block reason, first seen first."""
    found: list[str] = []
    for line in reason.splitlines()[1:]:
        for pattern in _CAUSE_PATTERNS:
            match = pattern.match(line)
            if match:
                text = match.group(1).strip()
                if text not in found:
                    found.append(text)
                break
    return found[:_MAX_CAUSES]


def blocked(store: TicketStore) -> list[dict]:
    """Each blocked ticket with the headline of its reason and its causes."""
    result = []
    for t in store.tickets:
        if t.status != "blocked":
            continue
        reason = t.blocked_reason or ""
        stage = t.blocked_stage or t.phase
        result.append(
            {
                "id": t.id,
                "title": t.title,
                "stage": _STAGES.get(stage, stage),
                "reason": reason.splitlines()[0] if reason else "no reason recorded",
                "causes": causes(reason),
            }
        )
    return result


def notes(details: list[str]) -> list[str]:
    """Run-level details: why the run stopped or what it could not do."""
    return [d.splitlines()[0] for d in details if d and not _TICKET_DETAIL.match(d)]
