"""Ticket model and on-disk state store.

The local state file is the source of truth for the build/verify loop; GitHub
sub-issues (when enabled) are a mirror. State is saved after every transition
so a crashed or credit-capped run can resume where it stopped.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATUSES = ("pending", "in_progress", "done", "blocked")


@dataclass
class Ticket:
    id: int
    title: str
    description: str
    test_assertion: str
    files_hint: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    _status: str = field(default="pending", repr=False)
    rounds: int = 0
    test_path: str | None = None
    github_issue: int | None = None
    blocked_reason: str | None = None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        if value not in STATUSES:
            raise ValueError(f"invalid ticket status {value!r}; expected one of {STATUSES}")
        self._status = value

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = d.pop("_status")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Ticket":
        d = dict(d)
        d["_status"] = d.pop("status", "pending")
        return cls(**d)


class TicketStore:
    def __init__(self, state_dir: Path, issue_ref: str):
        self.state_dir = Path(state_dir)
        self.issue_ref = issue_ref
        self.tickets: list[Ticket] = []
        self.branch: str | None = None
        self.plan_summary: str | None = None

    @property
    def state_file(self) -> Path:
        return self.state_dir / f"issue-{self.issue_ref}.json"

    def set_tickets(self, tickets: list[Ticket]) -> None:
        self.tickets = list(tickets)

    def pending(self) -> list[Ticket]:
        return [t for t in self.tickets if t.status in ("pending", "in_progress")]

    def _by_id(self) -> dict[int, Ticket]:
        return {t.id: t for t in self.tickets}

    def ready(self) -> list[Ticket]:
        """Pending tickets whose every dependency is done, in plan order.

        A ticket whose dependency is missing, blocked, or part of a cycle is
        never ready — the orchestrator blocks it explicitly rather than
        silently skipping it, so the run cannot deadlock.
        """
        by_id = self._by_id()
        return [
            t
            for t in self.pending()
            if all(dep in by_id and by_id[dep].status == "done" for dep in t.depends_on)
        ]

    def dependency_failure(self, ticket: Ticket) -> str | None:
        """A concrete, nameable reason this ticket can never run — or None."""
        by_id = self._by_id()
        for dep in ticket.depends_on:
            if dep not in by_id:
                return f"depends on unknown ticket {dep}"
            if by_id[dep].status == "blocked":
                return f"depends on ticket {dep} which is blocked"
        return None

    def unsatisfiable_reason(self, ticket: Ticket) -> str:
        return self.dependency_failure(ticket) or (
            "dependency cycle: no ordering of the remaining tickets can satisfy it"
        )

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "issue_ref": self.issue_ref,
            "branch": self.branch,
            "plan_summary": self.plan_summary,
            "tickets": [t.to_dict() for t in self.tickets],
        }
        self.state_file.write_text(json.dumps(payload, indent=2))

    def load(self) -> bool:
        if not self.state_file.exists():
            return False
        payload = json.loads(self.state_file.read_text())
        self.branch = payload.get("branch")
        self.plan_summary = payload.get("plan_summary")
        self.tickets = [Ticket.from_dict(d) for d in payload.get("tickets", [])]
        return True
