"""Per-run accounting: what a run actually cost.

Every model call is recorded with its role, model, wall-clock duration, outcome
and — when copilot reports it — token usage. Rollups are written to
`.issue-runner/usage-issue-<n>.json` (accumulating across resumed runs) and
appended as one JSON line per run to `.issue-runner/usage.log`, so per-role
model choices can be compared without re-reading terminal scrollback.

The `usage-` prefix matters: the ticket state files are `issue-<n>.json`, and
consumers glob `issue-*.json` for them, so a usage file must not match.

Production always uses copilot's JSON event stream, so tokens and the real
nano-AIU charge are recorded whether or not a display is attached. One CLI
invocation can make several model calls; `model_calls` counts them and the token
and credit figures are summed across all of them. Only the test-only plain
transport reports nothing, and then the totals stay None rather than being
reported as a misleading zero.
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


def format_aiu(nano_aiu: int) -> str:
    """Copilot bills in nano-AIU; show the AIU figure a user can compare."""
    return f"{nano_aiu / 1_000_000_000:.2f} AIU"


def _ticket_id(session: str | None) -> int | None:
    match = _TICKET_RE.search(session or "")
    return int(match.group(1)) if match else None


_SUMMED = ("input_tokens", "output_tokens", "cached_tokens", "nano_aiu")


def merge_usage(total: dict | None, new: dict | None) -> dict | None:
    """Fold one model_call_success payload into the invocation running total.

    Unknown stays unknown: a key nobody reported remains absent, so the ledger
    records None rather than a success-shaped zero. `model_calls` counts every
    model call seen, `costed_model_calls` only those that carried a real charge.
    """
    if new is None:
        return total
    merged = dict(total or {})
    for key in _SUMMED:
        if key in merged or key in new:
            merged[key] = _add(merged.get(key), new.get(key))
    merged["model_calls"] = merged.get("model_calls", 0) + (new.get("model_calls") or 0)
    merged["costed_model_calls"] = merged.get("costed_model_calls", 0) + (
        new.get("costed_model_calls") or 0
    )
    return merged


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
    cached_tokens: int | None
    nano_aiu: int | None
    model_calls: int | None = None  # model calls inside this one CLI invocation


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
                cached_tokens=usage.get("cached_tokens"),
                nano_aiu=usage.get("nano_aiu"),
                model_calls=usage.get("model_calls"),
            )
        )

    def _bucket(self, calls: list[CallRecord]) -> dict:
        bucket = {
            "calls": len(calls),
            "failed": sum(1 for c in calls if not c.ok),
            "seconds": round(sum(c.seconds for c in calls), 1),
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "nano_aiu": None,
            "model_calls": None,
        }
        for call in calls:
            bucket["input_tokens"] = _add(bucket["input_tokens"], call.input_tokens)
            bucket["output_tokens"] = _add(bucket["output_tokens"], call.output_tokens)
            bucket["cached_tokens"] = _add(bucket["cached_tokens"], call.cached_tokens)
            bucket["nano_aiu"] = _add(bucket["nano_aiu"], call.nano_aiu)
            bucket["model_calls"] = _add(bucket["model_calls"], call.model_calls)
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
            tokens = f"tokens: {incoming:,} in / {outgoing:,} out"
            if totals["cached_tokens"]:
                tokens += f" ({totals['cached_tokens']:,} cached)"
            parts.append(tokens)
        if totals["nano_aiu"] is not None:
            parts.append(f"credits: {format_aiu(totals['nano_aiu'])}")
        else:
            parts.append("credits: unknown (copilot reported none)")
        if totals["model_calls"] and totals["model_calls"] != totals["calls"]:
            parts.append(f"model calls: {totals['model_calls']}")
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
        "cached_tokens": None,
        "nano_aiu": None,
        "model_calls": None,
    }
    for bucket in buckets:
        for key in (*_SUMMED, "model_calls"):
            combined[key] = _add(combined[key], bucket.get(key))
    return combined
