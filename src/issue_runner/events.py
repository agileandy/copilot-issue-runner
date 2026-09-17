"""Typed event seam between the pipeline and any display.

The orchestrator and CopilotClient emit events here; consumers (the `--visual`
display bridge, a future `watch` mode, tests) subscribe. Headless runs pass no bus and
nothing changes. A failing subscriber never breaks the pipeline.

Event kinds and payloads:
    run_started         issue_ref, title
    phase               name ("plan"|"branch"|"build"|"finished"), detail?
    tickets_updated     tickets=[{id,title,status,rounds,blocked_reason}]
    ticket_started      ticket_id, title
    agent_call_started  role, ticket_id?, model?
    agent_output        role, ticket_id?, chunk
    agent_call_finished role, ticket_id?, elapsed, usage?
    verdict             ticket_id, verdict, reasons
    ticket_done         ticket_id, note
    ticket_blocked      ticket_id, reason
    stop_requested     message
    run_finished      done, blocked, branch, worktree?, pr_url?, usage?, budget?,
                      budget_exhausted?, plan_only?, stopped?,
                      worktree_state={path,branch,head,dirty,uncommitted},
                      artefacts={files,commits,pr_url}
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger("issue_runner")


@dataclass(frozen=True)
class RunEvent:
    kind: str
    payload: dict = field(default_factory=dict)


class EventBus:
    def __init__(self):
        self._subscribers = []

    def subscribe(self, handler) -> None:
        self._subscribers.append(handler)

    def emit(self, kind: str, **payload) -> None:
        event = RunEvent(kind, payload)
        for handler in list(self._subscribers):
            try:
                handler(event)
            except Exception:
                log.debug("event subscriber failed for %s", kind, exc_info=True)


def emit(bus, kind: str, **payload) -> None:
    """No-op-safe emit: `bus` may be None (headless run)."""
    if bus is not None:
        bus.emit(kind, **payload)


def ticket_snapshot(tickets) -> list[dict]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "rounds": t.rounds,
            "blocked_reason": t.blocked_reason,
        }
        for t in tickets
    ]
