"""Run-level AI credit budget: a stop rule, not a spend guarantee.

`max_ai_credits` is passed to the Copilot CLI as a per-call cap, but that cap is
soft — the CLI may overshoot it, and an invocation with no cap can make many
model calls and consume far more than one credit. So the run budget cannot
promise "this run will never cost more than N". What it can do is stop starting
new calls once the run has reached the configured limit.

Two quantities are tracked separately and never conflated:

- reserved: an *estimate* booked before a call whose real cost is not yet known
  (the per-call cap when one is set, otherwise 1). Purely a placeholder.
- measured: the actual `copilotUsage.total_nano_aiu` copilot reported, summed
  over every model call in the invocation.

`settle()` runs after each call and has three outcomes:

- complete: copilot reported a charge for every model call in the invocation.
  The reservation is released and replaced by the measured figure, including a
  genuine zero.
- partial: some model calls reported a charge and some did not, or the
  invocation was cut short by a timeout or crash after reporting one. The known
  charge is banked — it is real spend and a lower bound that counts towards the
  stop rule — *and* the reservation is retained to stand for the unknown
  remainder.
- unknown: nothing was reported. The reservation stands alone.

So a missing figure is never rendered as free, and a partial figure is never
rendered as the whole cost.
"""

NANO_PER_AIU = 1_000_000_000


class BudgetExhausted(RuntimeError):
    """Raised instead of starting a call once the run limit has been reached."""


class RunBudget:
    def __init__(self, limit: int | None = None, per_call: int = 1):
        self.limit = limit
        self.per_call = max(1, per_call)
        self.reserved = 0  # estimated credits held for calls of unknown cost
        self.measured_nano_aiu = 0  # actual charge copilot reported, in nano-AIU
        self.calls = 0
        self.measured_calls = 0  # copilot costed every model call in the invocation
        self.partial_calls = 0  # some of it was costed; the rest is still reserved
        self.unknown_calls = 0  # nothing was costed

    @property
    def cost_is_complete(self) -> bool:
        """True only when every call so far was costed in full by copilot."""
        return self.partial_calls == 0 and self.unknown_calls == 0

    @property
    def measured_aiu(self) -> float:
        return self.measured_nano_aiu / NANO_PER_AIU

    @property
    def spent(self) -> float:
        """Reserved estimate plus measured charge — the figure `check` gates on."""
        total = self.reserved + self.measured_aiu
        return int(total) if float(total).is_integer() else round(total, 6)

    @property
    def remaining(self) -> float | None:
        return None if self.limit is None else max(0, self.limit - self.spent)

    def check(self) -> None:
        if self.limit is None:
            return
        if self.spent + self.per_call > self.limit:
            raise BudgetExhausted(self._exhausted_message())

    def charge(self) -> None:
        """Book the per-call estimate before the call starts."""
        self.reserved += self.per_call
        self.calls += 1

    def settle(self, nano_aiu: int | None = None, complete: bool = True) -> None:
        """Reconcile a finished call against what copilot actually charged.

        `nano_aiu` is the charge copilot reported, which may be None. `complete`
        says whether that figure covers every model call in the invocation; an
        interrupted call is never complete, because more may have been billed
        after the transport died. An incomplete charge is banked as a lower
        bound and keeps its reservation for the unknown remainder.
        """
        if nano_aiu is None:
            self.unknown_calls += 1
            return
        self.measured_nano_aiu += int(nano_aiu)
        if complete:
            self.reserved = max(0, self.reserved - self.per_call)
            self.measured_calls += 1
        else:
            self.partial_calls += 1

    def status(self) -> dict:
        return {
            "limit": self.limit,
            "calls": self.calls,
            "measured_calls": self.measured_calls,
            "partial_cost_calls": self.partial_calls,
            "unknown_cost_calls": self.unknown_calls,
            "measured_nano_aiu": self.measured_nano_aiu,
            "measured_aiu": round(self.measured_aiu, 6),
            "reserved_aiu": self.reserved,
            "spent": self.spent,
            "remaining": self.remaining,
            "cost_is_complete": self.cost_is_complete,
        }

    def describe(self) -> str:
        prefix = "" if self.cost_is_complete else "at least "
        parts = [
            (
                f"measured: {prefix}{self.measured_aiu:.2f} AIU over "
                f"{self.measured_calls + self.partial_calls} calls"
            ),
            (
                f"incomplete cost: {self.partial_calls} partial, {self.unknown_calls} unknown "
                f"(reserved {self.reserved} AIU at {self.per_call}/call)"
            ),
        ]
        if self.limit is not None:
            parts.append(f"limit: {self.spent}/{self.limit}")
        return "budget — " + ", ".join(parts)

    def _exhausted_message(self) -> str:
        return (
            f"run credit limit reached: {self.spent}/{self.limit} credits across "
            f"{self.calls} calls ({self.measured_aiu:.2f} AIU charged over "
            f"{self.measured_calls} fully costed and {self.partial_calls} partly costed "
            f"calls; {self.unknown_calls} calls of unknown cost reserved at "
            f"{self.per_call} each) and the next call reserves {self.per_call}. "
            "This limit stops further calls; it is not a guarantee that actual spend "
            "stayed below it, because the per-call cap is soft and partial or unknown "
            "charges are only estimated."
        )
