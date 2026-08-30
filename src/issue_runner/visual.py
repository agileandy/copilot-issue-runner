"""ASCII snapshot renderer for the issue-runner pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_flow(report: Mapping[str, Any] | None) -> str:
    """Render the issue runner pipeline as a compact status snapshot."""
    payload = report or {}

    plan = str(payload.get("plan", "pending"))
    branch = str(payload.get("branch", "pending"))
    commit = str(payload.get("commit", "pending"))
    tickets = payload.get("tickets", []) or []

    ticket_lines = []
    for item in tickets:
        if isinstance(item, Mapping):
            ticket_id = item.get("id", "?")
            ticket_status = item.get("status", "pending")
        else:
            ticket_id = getattr(item, "id", "?")
            ticket_status = getattr(item, "status", "pending")
        ticket_lines.append(f"  ticket #{ticket_id}: {ticket_status}")

    if not ticket_lines:
        ticket_lines = ["  tickets: none"]

    lines = [
        "issue pipeline",
        f"plan: {plan}",
        "   ↓",
        f"branch: {branch}",
        "   ↓",
        "per-ticket build/verify:",
        *ticket_lines,
        "   ↓",
        f"commit: {commit}",
    ]
    return "\n".join(lines)


__all__ = ["render_flow"]
