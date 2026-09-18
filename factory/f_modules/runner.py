"""The ``Run`` object and the single phase primitive.

This module and ``session.py`` between them encode most of §14.1's identity set. Two rules in
particular are structural here rather than advisory:

**Success must be earned.** A phase is constructed ``FAIL`` and only a clean exit flips it to
``SUCCESS``. An exception leaves it failed, records an ``error`` event, finalizes the session and
aborts the run.

**Run acceptance is a second, separate question** (§1.4). ``run.finish(accepted=...)`` takes
acceptance *explicitly* and settles the persisted status, the terminal banner and the process exit
code together, so the three can never disagree::

    return run.finish(accepted=test.passed and review.approved,
                      reason="the suite or the review never came back clean")

The distinction is the most load-bearing subtlety in the system. A test phase that ran a red suite
**did its job** — the phase succeeded and the run must not. There is deliberately no ``succeeded``
property anywhere in this module: the anti-pattern it exists to prevent is exactly such a property,
with side effects, evaluated before the caller's ``and test.passed`` — recording a green run in the
database and on the terminal while exiting 1, so everyone reading the trace saw success and only CI
saw the truth.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from f_modules.console import Console
from f_modules.data_types import (
    AgentCall,
    EnvelopeBase,
    EventType,
    FactoryConfig,
    PhaseParams,
    PhaseRecord,
    PhaseStatus,
    SessionStatus,
)
from f_modules.tracer import Tracer
from f_modules.utils import new_id


class RunKilled(Exception):
    """A signal, converted into a normal exception so the run can unwind (§9.9)."""


class PhaseFailed(Exception):
    """An agent phase whose envelope or gates were unacceptable (§1.3)."""


AgentExecutor = Callable[["Phase", AgentCall], EnvelopeBase]
"""Signature of the parse-and-gate pipeline that runs an agent turn.

Supplied by ``agents.py``. ``Run`` takes it as a dependency rather than importing it, so the phase
primitive stays testable without spawning anything and the two modules do not import each other.
"""


class Phase:
    """The handle a phase body is given — ``ph`` in every FW script."""

    def __init__(self, run: Run, record: PhaseRecord) -> None:
        self.run = run
        self.record = record

    @property
    def phase_id(self) -> str:
        return self.record.phase_id

    def log(self, message: str = "", **fields: Any) -> None:
        """Narrate. Goes to the terminal and the trace through the one helper (§9.8)."""
        text = message or ", ".join(f"{k}={v}" for k, v in fields.items())
        self.run.console.say(f"{self.record.name}: {text}", phase_id=self.phase_id, **fields)

    def event(self, type: EventType, name: str = "", **payload: Any) -> None:
        self.run.tracer.event(type, phase_id=self.phase_id, name=name, payload=payload)

    def call(self, call: AgentCall) -> EnvelopeBase:
        """Run one agent turn: prompt in, typed envelope out, gates verified.

        Raises rather than returning a bad envelope, so §1.3's extra conditions for an ``agent``
        phase — parsed against the declared type, ``status != "fail"``, and **all** gates green —
        travel down the same exception path that leaves the phase failed.
        """
        if self.run.agent_executor is None:
            raise RuntimeError(
                "no agent executor is wired into this Run; agents.py supplies one, and a phase "
                "cannot call an agent without it"
            )
        return self.run.agent_executor(self, call)


class Run:
    """One execution, identified by an ``f_id``. Can be joined by later FWs."""

    def __init__(
        self,
        f_id: str,
        config: FactoryConfig,
        tracer: Tracer,
        console: Console,
        engineer: str = "",
        start_seq: int = 0,
        agent_executor: AgentExecutor | None = None,
        request: str = "",
    ) -> None:
        self.f_id = f_id
        self.config = config
        self.tracer = tracer
        self.console = console
        self.engineer = engineer
        self.request = request
        """The engineer's original ask — what ``{{prompt}}`` renders to absent a per-call override."""
        self.agent_executor = agent_executor

        self._seq = start_seq
        """The highest sequence number already in the trace.

        A joined run continues from here rather than restarting at 1 (§5.8): restarting would
        collide on both ordering and phase id, silently overwriting the first run's rows.
        """

        self.total_tokens = 0
        self.total_cost = 0.0
        self._finalized = False

    # -- spend ------------------------------------------------------------------------------

    def record_spend(self, tokens: int, cost: float) -> None:
        """Accumulate spend. Retries cost money, so this sums (§8.8).

        Context *occupancy* is a different number entirely and is overwritten, not summed — it
        lives on the agent session row, not here.
        """
        self.total_tokens += tokens
        self.total_cost += cost

    # -- the phase primitive ----------------------------------------------------------------

    @contextmanager
    def phase(self, params: PhaseParams) -> Iterator[Phase]:
        """Open one phase.

        ``params`` has already rejected a blank or name-echoing description at *its* construction
        (§10.2), which is earlier than here — the phase never opens, so a run that would produce a
        meaningless trace never enters the trace at all.
        """
        self._seq += 1
        record = PhaseRecord(
            phase_id=new_id("ph"),
            f_id=self.f_id,
            seq=self._seq,
            name=params.name,
            kind=params.kind,
            owner=params.owner,
            description=params.description,
            retries=params.retries,
        )
        # Constructed fail, persisted fail. Nothing below has to remember to set it.
        self.tracer.phase_start(record)
        self.tracer.event(
            EventType.PHASE_START,
            phase_id=record.phase_id,
            name=record.name,
            payload={"kind": record.kind.value, "owner": record.owner,
                     "description": record.description, "seq": record.seq},
        )
        self.console.say(
            f"[{record.seq}] {record.name} ({record.kind.value}/{record.owner}) "
            f"— {record.description}",
            phase_id=record.phase_id,
        )

        handle = Phase(self, record)
        try:
            yield handle
        except BaseException as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            self.tracer.event(
                EventType.ERROR,
                phase_id=record.phase_id,
                name=type(exc).__name__,
                payload={"error": str(exc), "traceback": traceback.format_exc()},
            )
            self.tracer.phase_end(record)  # still FAIL; nothing earned it otherwise
            self.console.error(f"{record.name}: {record.error}", phase_id=record.phase_id)
            self.abort(record.error)
            raise
        else:
            record.status = PhaseStatus.SUCCESS
            self.tracer.phase_end(record)
            self.tracer.event(
                EventType.PHASE_END,
                phase_id=record.phase_id,
                name=record.name,
                payload={"status": record.status.value},
            )

    # -- termination ------------------------------------------------------------------------

    def finish(self, accepted: bool, reason: str = "") -> int:
        """Settle the persisted status, the banner and the exit code — all three, together.

        ``accepted`` is the FW's **own** acceptance criterion and is passed explicitly. It is not
        derived from phase statuses, because every phase succeeding and the run being acceptable are
        two different questions (§1.4).

        Returns the process exit code, so an FW's ``main()`` ends with ``return run.finish(...)``.
        """
        status = SessionStatus.SUCCESS if accepted else SessionStatus.FAIL
        self._finalize(status)

        if accepted:
            self.console.banner(f"RUN OK  {self.f_id}")
        else:
            self.console.banner(f"RUN FAILED  {self.f_id}: {reason or 'not accepted'}")
        return 0 if accepted else 1

    def abort(self, reason: str) -> None:
        """Finalize as failed. Called when a phase body raised, before the exception propagates."""
        if self._finalized:
            return
        self._finalize(SessionStatus.FAIL)
        self.console.banner(f"RUN FAILED  {self.f_id}: {reason}")

    def _finalize(self, status: SessionStatus) -> None:
        if self._finalized or self.tracer.closed:
            return
        self._finalized = True
        # §9.9: a trace must never claim work is in flight that is already dead.
        self.tracer.close_open_processes()
        self.tracer.session_finish(status, self.total_tokens, self.total_cost)

    def finalize_if_abandoned(self) -> None:
        """Last resort, for a run that ended without ever calling :meth:`finish`.

        Success is earned, so an abandoned run is a failed one — a session left reading ``running``
        forever is the outcome this exists to prevent. Registered with ``atexit`` to cover the one
        window a phase context manager cannot: a signal or an unhandled error arriving *between*
        phases.
        """
        if self._finalized or self.tracer.closed:
            return
        self._finalize(SessionStatus.FAIL)
        self.console.error(f"{self.f_id}: the run ended without calling finish()")

    def close(self) -> None:
        """Finish the run's bookkeeping and shut the writer down, in that order.

        The ordering matters: closing the tracer first would leave an unfinalized session reading
        ``running`` forever with no writer left to correct it, which is precisely what §9.9 forbids.
        A ``Run`` therefore owns its tracer's lifetime — callers close the run, not the tracer.
        """
        self.finalize_if_abandoned()
        self.tracer.close()
