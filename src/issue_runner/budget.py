"""Run-level AI credit budget.

`max_ai_credits` caps a single Copilot call; nothing capped the whole run, so a
badly-scoped issue could burn far more than intended across planner + N tickets
× tester/coder/verifier rounds.

The Copilot CLI does not report how much credit a call actually consumed, so the
budget is enforced on a worst-case estimate: a call is assumed to cost the
per-call cap (`max_ai_credits`) when one is set, and 1 otherwise. The check runs
BEFORE each call, so the budget is never exceeded — it is a floor on what the
runner will attempt, not an exact meter.
"""


class BudgetExhausted(RuntimeError):
    """Raised instead of making a call that the run budget cannot afford."""


class RunBudget:
    def __init__(self, limit: int | None = None, per_call: int = 1):
        self.limit = limit
        self.per_call = max(1, per_call)
        self.spent = 0
        self.calls = 0

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.spent)

    def check(self) -> None:
        if self.limit is None:
            return
        if self.spent + self.per_call > self.limit:
            raise BudgetExhausted(
                f"run credit budget exhausted: {self.spent}/{self.limit} credits spent "
                f"over {self.calls} model calls; the next call needs {self.per_call}"
            )

    def charge(self) -> None:
        self.spent += self.per_call
        self.calls += 1
