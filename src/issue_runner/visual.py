"""ASCII snapshot renderer for the issue-runner pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _status_label(status: Any) -> str:
    label = str(status or "pending").lower()
    if label == "pending":
        return "pending"
    if label == "in_progress":
        return "in_progress"
    if label == "done":
        return "done"
    if label == "blocked":
        return "blocked"
    return label


def _render_ticket(item: Any) -> str:
    if isinstance(item, Mapping):
        ticket_id = item.get("id", "?")
        ticket_status = item.get("status", "pending")
        title = item.get("title") or item.get("summary")
        assertion = item.get("assertion") or item.get("test_assertion")
    else:
        ticket_id = getattr(item, "id", "?")
        ticket_status = getattr(item, "status", "pending")
        title = getattr(item, "title", None) or getattr(item, "summary", None)
        assertion = getattr(item, "assertion", None) or getattr(item, "test_assertion", None)

    status = _status_label(ticket_status)
    if title or assertion:
        parts = [f"ticket {ticket_id}", status]
        if title:
            parts.append(str(title))
        if assertion:
            parts.append(f"assert: {assertion}")
        return f"  {' | '.join(parts)}"
    return f"  ticket #{ticket_id}: {status}"


def render_flow(report: Mapping[str, Any] | None) -> str:
    """Render the issue runner pipeline as a compact status snapshot."""
    payload = report or {}

    plan = str(payload.get("plan", "pending"))
    branch = str(payload.get("branch", "pending"))
    commit = str(payload.get("commit", "pending"))
    tickets = payload.get("tickets", []) or []

    ticket_lines = [_render_ticket(item) for item in tickets]
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
