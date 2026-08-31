"""Per-run accounting: what a run actually cost.

Every model call is recorded with its role, model, wall-clock duration, outcome
and — when copilot reports it — token usage. Rollups are written to
`.issue-runner/usage-issue-<n>.json` (accumulating across resumed runs) and
appended as one JSON line per run to `.issue-runner/usage.log`, so per-role
model choices can be compared without re-reading terminal scrollback.

The `usage-` prefix matters: the ticket state files are `issue-<n>.json`, and
consumers glob `issue-*.json` for them, so a usage file must not match.

Token counts only exist on the streaming path (`--output-format json`, i.e. when
an event bus is attached). On the plain path copilot reports nothing, so token
totals stay None rather than being reported as a misleading zero.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_TICKET_RE = re.compile(r"-t(\d+)$")

USAGE_LOG = "usage.log"


def format_duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _ticket_id(session: str | None) -> int | None:
    match = _TICKET_RE.search(session or "")
    return int(match.group(1)) if match else None


def _add(left: int | None, right: int | None) -> int | None:
    """Sum that keeps None when nothing was ever reported."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


@dataclass(frozen=True)
class CallRecord:
    role: str
    model: str | None
    effort: str | None
    session: str | None
    ticket_id: int | None
    seconds: float
    ok: bool
    input_tokens: int | None
    output_tokens: int | None


class UsageLedger:
    def __init__(self):
        self.calls: list[CallRecord] = []
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def record(
        self,
        role: str,
        model: str | None,
        effort: str | None,
        session: str | None,
        seconds: float,
        ok: bool,
        usage: dict | None,
    ) -> None:
        usage = usage or {}
        self.calls.append(
            CallRecord(
                role=role,
                model=model,
                effort=effort,
                session=session,
                ticket_id=_ticket_id(session),
                seconds=round(float(seconds), 1),
                ok=bool(ok),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        )

    def _bucket(self, calls: list[CallRecord]) -> dict:
        bucket = {
            "calls": len(calls),
            "failed": sum(1 for c in calls if not c.ok),
            "seconds": round(sum(c.seconds for c in calls), 1),
            "input_tokens": None,
            "output_tokens": None,
        }
        for call in calls:
            bucket["input_tokens"] = _add(bucket["input_tokens"], call.input_tokens)
            bucket["output_tokens"] = _add(bucket["output_tokens"], call.output_tokens)
        return bucket

    def totals(self) -> dict:
        return self._bucket(self.calls)

    def by_role(self) -> dict[str, dict]:
        roles = {}
        for call in self.calls:
            roles.setdefault(call.role, []).append(call)
        return {role: self._bucket(calls) for role, calls in roles.items()}

    def by_ticket(self) -> dict[int | None, dict]:
        tickets = {}
        for call in self.calls:
            tickets.setdefault(call.ticket_id, []).append(call)
        return {ticket: self._bucket(calls) for ticket, calls in tickets.items()}

    def summary_line(self) -> str:
        totals = self.totals()
        parts = [
            f"calls: {totals['calls']}",
            f"duration: {format_duration(totals['seconds'])}",
        ]
        if totals["failed"]:
            parts.append(f"failed: {totals['failed']}")
        if totals["input_tokens"] is not None or totals["output_tokens"] is not None:
            incoming = totals["input_tokens"] or 0
            outgoing = totals["output_tokens"] or 0
            parts.append(f"tokens: {incoming} in / {outgoing} out")
        roles = ", ".join(
            f"{role}={data['calls']}" for role, data in sorted(self.by_role().items())
        )
        if roles:
            parts.append(f"by-role: {roles}")
        return "usage — " + ", ".join(parts)

    def run_entry(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "totals": self.totals(),
            "by_role": self.by_role(),
            # JSON object keys must be strings; None becomes "" (non-ticket calls)
            "by_ticket": {
                ("" if k is None else str(k)): v
                for k, v in sorted(self.by_ticket().items(), key=lambda kv: (kv[0] is None, kv[0]))
            },
            "calls": [asdict(c) for c in self.calls],
        }

    def save(self, state_dir: Path, issue_ref: str) -> Path:
        """Append this run to the issue's usage file and the repo-level log."""
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"usage-issue-{issue_ref}.json"

        previous_runs = []
        if path.exists():
            try:
                previous_runs = json.loads(path.read_text()).get("runs", [])
            except (OSError, ValueError):
                previous_runs = []  # a corrupt file must never fail a run

        entry = self.run_entry()
        runs = [*previous_runs, entry]
        payload = {
            "issue_ref": issue_ref,
            "runs": runs,
            "totals": _combine([r.get("totals", {}) for r in runs]),
        }
        path.write_text(json.dumps(payload, indent=2))

        line = json.dumps({"issue_ref": issue_ref, **entry, "calls": len(self.calls)})
        with (state_dir / USAGE_LOG).open("a") as log_file:
            log_file.write(line + "\n")
        return path


def _combine(buckets: list[dict]) -> dict:
    combined = {
        "calls": sum(b.get("calls", 0) for b in buckets),
        "failed": sum(b.get("failed", 0) for b in buckets),
        "seconds": round(sum(b.get("seconds", 0) for b in buckets), 1),
        "input_tokens": None,
        "output_tokens": None,
    }
    for bucket in buckets:
        combined["input_tokens"] = _add(combined["input_tokens"], bucket.get("input_tokens"))
        combined["output_tokens"] = _add(combined["output_tokens"], bucket.get("output_tokens"))
    return combined
