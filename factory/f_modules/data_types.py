"""Every schema the factory uses: config, envelopes, phases, events, requests.

§14.3 puts this module first, and the reason is practical: config, phases, envelopes, events and
requests are referenced by every other module, so building them second means building everything
twice.

It is also where the system's vocabulary is fixed. Three phase kinds, four phase statuses, ten
event types, seven thinking levels, seven canonical tool names — all defined once, here, so no
other module gets to invent a fourth status or an eleventh event.

Two invariants are enforced *by the types themselves* rather than left to the code that uses them:

- **Success must be earned.** :class:`PhaseRecord` is constructed ``FAIL``. Nothing has to remember
  to set it; something has to earn the right to change it.
- **Every phase earns a description** (§10.2). :class:`PhaseParams` rejects a blank description, and
  rejects one that merely restates the phase name, *at construction time* — before the phase opens,
  so a run that would produce a meaningless trace never enters the trace at all.

§5.5's synced triad starts here. This module is leg (a). Legs (b) and (c) — the JSON example under
``## Report`` in each agent's ``user.md``, and ``output_type=`` at every call site — must move in
the same edit as this file. Drift between them taxes every call with a correction round-trip: the
agent is told to emit a shape the parser was always going to reject.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------------------------
# Vocabulary (Appendix A)
# ---------------------------------------------------------------------------------------------


class PhaseKind(StrEnum):
    """Three kinds, three swim lanes (§1.2).

    The kind determines who is *accountable*, not what the code does. A ``code`` operation is never
    buried inside an ``agent`` phase: if a commit happens there is a commit phase, and if a suite
    runs there is a test phase. Hiding determinism inside an agent phase makes the trace lie about
    who did what.
    """

    ENGINEER = "engineer"
    """System input — who asked, and for what. Always the first phase."""

    AGENT = "agent"
    """Prompt in, typed envelope out, gates verified."""

    CODE = "code"
    """Deterministic work: commit, diff, test, migrate."""


class PhaseStatus(StrEnum):
    """``queued -> running -> success | fail``, where the default is ``fail`` (§1.3)."""

    QUEUED = "queued"
    """Declared but never entered. **Not** a failure — see :func:`is_reportable_failure`."""

    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"


class SessionStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"


class EnvelopeStatus(StrEnum):
    """An agent's report of whether *its task* completed — not whether the verdict was favourable.

    A reviewer that completes a review reports ``success`` with ``approved=False`` (§11.3).
    """

    SUCCESS = "success"
    FAIL = "fail"


class EventType(StrEnum):
    """The ten event types (§9.5). Every event carries both ``f_id`` and ``phase_id``."""

    PHASE_START = "phase_start"
    AGENT_START = "agent_start"
    TOOL_CALL = "tool_call"
    """The only type that spans real elapsed time."""

    HANDOFF = "handoff"
    GATE_PASS = "gate_pass"
    GATE_FAIL = "gate_fail"
    LOG = "log"
    AGENT_END = "agent_end"
    PHASE_END = "phase_end"
    ERROR = "error"


class Thinking(StrEnum):
    """The canonical ladder (§8.3). Each adapter maps it to its own mechanism.

    Guidance: ``high``/``xhigh`` for planners and reviewers, ``medium`` for builders, ``low`` for
    mechanical recon.
    """

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


CANONICAL_TOOLS: tuple[str, ...] = ("read", "bash", "edit", "write", "grep", "find", "ls")
"""Tool names the harness speaks (§8.4).

Adapters translate these to their backend's spelling; unknown names — including extension and MCP
tool ids — pass through unchanged, so this tuple is a vocabulary, not an allowlist.
"""


def is_reportable_failure(status: PhaseStatus) -> bool:
    """Whether a phase status should be reported as a failure.

    ``QUEUED`` means *declared but never entered*. §1.3 is explicit that it is not a failure and
    must not be reported as one, which is easy to get wrong when the obvious test is
    ``status != SUCCESS``.
    """
    return status is PhaseStatus.FAIL


# ---------------------------------------------------------------------------------------------
# Config (Part 3)
# ---------------------------------------------------------------------------------------------

# Config is authored by humans and read by machines, so unknown keys are rejected rather than
# ignored. The motivating case is severe: a roster with `writs: []` instead of `writes: []` would,
# under a permissive model, leave `writes` unset — and unset means *unrestricted* (§4.3). A typo
# would silently hand an agent the whole repository. Forbidding extras turns that into a startup
# error, which §3.2 already insists is where roster problems belong.
_STRICT = ConfigDict(extra="forbid", frozen=True)


class PromptEngineering(BaseModel):
    """Paths to the two-file prompt split (§11.1)."""

    model_config = _STRICT

    system: str
    """Identity — Purpose and Instructions. Static; changes when the agent's role changes."""

    user: str
    """Task shape — Variables, Task, Report. Templated; changes when the contract changes."""


class Defaults(BaseModel):
    """Roster-wide defaults that each agent entry merges over, key by key (§3.2)."""

    model_config = _STRICT

    coding_agent: str = "copilot"
    model: str = "github-copilot/claude-opus-5"
    thinking: Thinking = Thinking.MEDIUM
    color: str = ""
    """Hex lane swatch; empty means the UI picks from its palette."""

    harness_engineering: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=lambda: list(CANONICAL_TOOLS))
    protected_files: list[str] = Field(default_factory=list)
    """Off-limits unless an agent names the path in its own ``writes`` (§4.3)."""

    data_dir: str = "factory/f_data"


class Observability(BaseModel):
    model_config = _STRICT

    db: str = "factory/f_data/sssf.db"
    poll_ms: int = 500


class AgentSpec(BaseModel):
    """One roster entry *as authored*, before defaults are merged in.

    Every inheritable field is optional here and required on :class:`ResolvedAgent`. That split is
    deliberate: it makes "unset, so inherit" and "set to this value" two different states in the
    type system, so the merge in ``agents.py`` cannot quietly skip a key.
    """

    model_config = _STRICT

    name: str
    """What FWs reference. FWs name *this*, never a model (§3.1)."""

    purpose: str = ""
    prompt_engineering: PromptEngineering

    coding_agent: str | None = None
    model: str | None = None
    thinking: Thinking | None = None
    color: str | None = None
    harness_engineering: list[str] | None = None
    tools: list[str] | None = None

    writes: list[str] | None = None
    """The write boundary (§4.3), and a genuine tri-state:

    - ``None``  — unrestricted, except ``protected_files``
    - ``[]``    — repository-read-only; may still write its own handoff files
    - ``[...]`` — only these paths

    ``None`` and ``[]`` mean opposite things, so this field must never be given ``[]`` as a
    convenience default and must never be normalised on the way through.
    """


class ResolvedAgent(BaseModel):
    """One roster entry after merging over :class:`Defaults` — every inheritable field populated."""

    model_config = _STRICT

    name: str
    purpose: str
    prompt_engineering: PromptEngineering
    coding_agent: str
    model: str
    thinking: Thinking
    color: str
    harness_engineering: list[str]
    tools: list[str]

    writes: list[str] | None
    """Still tri-state after the merge — see :attr:`AgentSpec.writes`. There is no default here on
    purpose: ``None`` is a meaningful value, not an absent one, so it must be passed explicitly."""

    @property
    def is_unrestricted(self) -> bool:
        """True when this agent may write anywhere outside ``protected_files`` (§4.3)."""
        return self.writes is None

    @property
    def is_repo_read_only(self) -> bool:
        """True when this agent may write nothing in the repository — but still its handoff files."""
        return self.writes == []


class FactoryConfig(BaseModel):
    """The roster (Part 3.1). Identity only — never output types (§3.3)."""

    model_config = _STRICT

    defaults: Defaults = Field(default_factory=Defaults)
    observability: Observability = Field(default_factory=Observability)
    agents: list[AgentSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Envelopes (§5.2, §5.3)
# ---------------------------------------------------------------------------------------------

# Envelopes come from a stochastic process, so unknown keys are *ignored* rather than rejected —
# the opposite of the config rule above, and for the opposite reason. Rejecting a field the agent
# volunteered would spend a correction round (§5.7) to remove information nobody was going to read.
# Frozen because an envelope is the record of what an agent claimed; editing it after the fact
# would make the trace a story rather than evidence.
_ENVELOPE = ConfigDict(extra="ignore", frozen=True)


class EnvelopeBase(BaseModel):
    """An agent's only structured output (§5.2).

    A structurally valid envelope with ``status="fail"`` still fails the phase.
    """

    model_config = _ENVELOPE

    status: EnvelopeStatus
    """The only required field."""

    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""

    @property
    def failed(self) -> bool:
        return self.status is EnvelopeStatus.FAIL


# `commit_message` is deliberately NOT on the base (§5.4). It describes its author's own work
# product — PlanOutput's describes the spec document, BuildOutput's the code, DocumentOutput's the
# write-up. Hoisting it to the base is what lets a chain reuse one agent's words for another
# agent's diff. It defaults empty, so every commit phase needs a fallback.


class GenericOutput(EnvelopeBase):
    """No additions. The fallback type for an agent with no specific contract."""


class PlanOutput(EnvelopeBase):
    commit_message: str = ""
    """Describes the spec document this agent wrote — not the code that implements it."""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""
    """Describes the code this agent wrote."""


class ScoutFinding(BaseModel):
    model_config = _ENVELOPE

    file: str
    note: str


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    model_config = _ENVELOPE

    requirement: str
    met: bool
    evidence: str = ""


class ReviewOutput(EnvelopeBase):
    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)


class DocumentOutput(EnvelopeBase):
    document_path: str = ""
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""
    """Describes the write-up this agent produced."""


# --- Adapters: code results shaped as envelopes (§5.3) ---
#
# Agents hand each other typed envelopes; code blocks return native result objects. These two
# adapt a code result into the *same door* an agent's report came through, so replacing a tester
# agent with a subprocess leaves the repair loop completely unchanged. Only the FW script knows
# the difference.


class VerifyOutput(EnvelopeBase):
    """A deterministic verification result, shaped as an envelope."""

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


class ChangesOutput(EnvelopeBase):
    """A git change-set capture, shaped as an envelope."""

    base: str = ""
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""


# ---------------------------------------------------------------------------------------------
# Param objects (§10.1)
# ---------------------------------------------------------------------------------------------
#
# "Any function with more than 4 parameters takes one concrete data type instead." This is why
# PhaseParams, AgentCall, ChangeCapture and QualityCheckSpec exist. It makes every call site
# self-documenting and every signature change additive.

_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "for", "of", "and", "or", "in", "on", "it", "its", "this", "that"}
)


def _significant_words(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    return {w for w in cleaned.split() if w and w not in _STOPWORDS}


class PhaseParams(BaseModel):
    """Everything needed to open one phase.

    The description is validated here rather than in the runner because §10.2 requires rejection at
    **construction time**, before the phase opens — so it fails before a run is already in the
    trace with a meaningless label attached to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: PhaseKind
    owner: str
    """The human's name for ``engineer``, an agent name for ``agent``, a short actor label such as
    ``git``, ``quality`` or ``db`` for ``code``."""

    description: str
    """The **only** intent the trace, the console and the UI ever show — everything else is ids,
    statuses and timings. Good: "Put the spec on record before any code exists to blur it"."""

    retries: int = 0
    """Gate-correction budget for this phase (§5.7). Distinct from the JSON-repair budget, which is
    a fixed constant, and from a chain's fix loop, which belongs to the FW."""

    @model_validator(mode="after")
    def _description_must_earn_its_place(self) -> PhaseParams:
        if not self.description.strip():
            raise ValueError(
                f"phase {self.name!r} has a blank description; the description is the only "
                "intent the trace ever shows"
            )

        described = _significant_words(self.description)
        named = _significant_words(self.name)
        if described and described <= named:
            raise ValueError(
                f"phase {self.name!r} has a description that only restates its name: "
                f"{self.description!r}. Say why the phase exists, not what it is called — "
                'e.g. "Put the spec on record before any code exists to blur it".'
            )
        return self


class AgentCall(BaseModel):
    """One invocation of an agent inside an ``agent`` phase.

    ``output_type`` lives here, at the call site, and never in config (§3.3): config defines
    *identity*, the call defines *use*, and the same agent can return different types in different
    FWs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    output_type: type[EnvelopeBase]
    prompt: str | None = None
    """Per-call override for ``{{prompt}}``; ``None`` uses the engineer's original ask (§5.6)."""

    previous: EnvelopeBase | None = None
    """Rendered into ``{{previous_envelope}}`` as JSON, or ``(none)``."""

    gates: list[Callable[..., "GateReport"]] = Field(default_factory=list)


class ChangeCapture(BaseModel):
    """Parameters for a deterministic "what changed" capture (§7.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = "main"
    """The reference the change-set is taken against. The *base* is resolved from it, not assumed —
    and the resolution records why (see :class:`ChangeBaseReason`)."""

    diff_path: str = ""
    """Where the diff artifact is written. Empty means the caller lets the harness choose."""

    max_diff_bytes: int = 200_000
    """The diff is truncated at this bound with an explicit marker and the command to get the
    rest — a silently truncated diff is worse than a short one."""

    include_untracked: bool = True
    """Untracked files are absent from ``git diff`` by construction, so they are named explicitly
    in the artifact rather than going silently missing."""


class ChangeBaseReason(StrEnum):
    """Why a particular base was chosen (§7.5).

    A diff is only as trustworthy as the thing it was taken against, so the reason travels in the
    trace instead of leaving the reader to infer it.
    """

    AHEAD_OF_REF = "every commit since the ref, plus the working tree"
    DIRTY_TREE = "the uncommitted working tree"
    CLEAN_TREE = "falling back to the last commit"
    NO_PARENT = "clean tree, no parent commit"


# ---------------------------------------------------------------------------------------------
# Gates (§6)
# ---------------------------------------------------------------------------------------------


class GateCheck(BaseModel):
    """One item examined by a gate.

    Passing checks are recorded too (§6.2), which is the whole point: a green gate then *says what
    it verified*. ``"plan.md: exists, 2.1KB"`` is evidence; ``passed: true`` is a rumour.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: str
    ok: bool
    note: str = ""
    """On a failed check this doubles as the reason sent back to the agent, so evidence and
    correction are the same text."""


class GateReport(BaseModel):
    """What one gate observed. Accumulates one check per item examined (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    gate: str = ""
    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> GateReport:
        """Append a check and return self, so a gate is a loop and a return."""
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------------------------
# Deterministic code blocks (§7.2)
# ---------------------------------------------------------------------------------------------


class QualityCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    area: str
    operation: str
    argv: list[str]
    """An argv list, never a shell string — no quoting bugs, no shell injection. Binaries are
    called by bare name so the trace does not bake in one machine's paths."""

    timeout_seconds: int = 600

    configured: bool = True
    """False for a block nobody has written yet (§7.2).

    A placeholder fails loudly by design, but a *fix loop* cannot tell that failure from a real one
    — so it would hand "no test command configured" to a builder, three times, and the builder would
    change code that was never the problem. The flag is what lets the chain refuse instead.
    """


class QualityCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    area: str
    operation: str
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str = ""
    """The full log, always on disk."""

    output_tail: str = ""
    """The verbatim tail of stdout+stderr, carried **inside** the envelope (§7.3) — a builder
    cannot open a log file it was never handed. Deliberately raw and unparsed: every runner formats
    failures differently and a generic parser would be confidently wrong."""


class QualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[QualityCheckResult] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# The provider abstraction (§8.2, §8.8, §8.9)
# ---------------------------------------------------------------------------------------------


class Usage(BaseModel):
    """Provider-neutral token usage for a single turn (§8.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: int = 0
    """Excludes cache reads, which bill at their own cheaper rate."""

    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    reasoning: int = 0
    """The thinking *share of output*, not a fifth component. Reasoning is always ``<= output`` and
    the four components always sum to the total, so this is reported nested under output and
    **never added** to it."""

    total: int = 0
    """The provider's reported total where one exists; otherwise the sum of the four components.
    Cached prompt is still prompt."""

    cost: float = 0.0
    """What this turn cost in total. Always meaningful."""

    # Per-component costs, where the backend breaks them out. Pi does; Copilot does not, and a
    # zero here would read as "input was free" rather than "not reported" — so consumers must
    # check :attr:`has_cost_breakdown` before rendering them as a breakdown.
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0

    @property
    def components_sum(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    @property
    def has_cost_breakdown(self) -> bool:
        """Whether the per-component costs were reported rather than defaulted."""
        return bool(
            self.input_cost or self.output_cost or self.cache_read_cost or self.cache_write_cost
        )


class ToolCallRecord(BaseModel):
    """One *real* tool call, folded from a backend's announce/start/update/end events (§8.9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result_snippet: str = ""

    label: str = ""
    """A one-line human label, e.g. ``bash: ls -la src``."""

    started_at: datetime | None = None
    ended_at: datetime | None = None
    """The call's real span, in dedicated fields so a UI can lay tool calls on a time axis without
    parsing payload JSON.

    Not every backend timestamps its own tool events — Pi does not — so the adapter is expected to
    stamp these from its own clock at the moment each event is *read*. That is only correct while
    the adapter is streaming; a buffered adapter makes every span a lie.
    """


class AgentRequest(BaseModel):
    """What the harness asks a backend to do (§8.2).

    Harness-managed path fields live here so the harness can forward them without knowing which
    backend is active. An adapter ignores fields it does not need.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str
    system_prompt: str = ""
    model: str = ""
    thinking: Thinking = Thinking.MEDIUM
    session_id: str = ""
    cwd: str = ""
    tools: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    session_dir: str = ""
    raw_output_path: str = ""


class AgentResult(BaseModel):
    """What a backend returned (§8.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    returncode: int = 0
    session_id: str = ""
    tokens: int = 0
    cost: float = 0.0
    usage: Usage = Field(default_factory=Usage)

    context_tokens: int = 0
    """Context occupancy: the window's state after the last *valid* turn. Overwritten, never
    summed — and never overwritten by an aborted or errored turn, whose usage is untrustworthy."""

    context_window: int = 0
    """0 when unknown."""


# ---------------------------------------------------------------------------------------------
# Trace records (§9.3)
# ---------------------------------------------------------------------------------------------


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    f_id: str
    f_name: str = ""
    """Accumulates chained FW names in run order, e.g. ``"f_plan + f_build_test"``."""

    request: str = ""
    status: SessionStatus = SessionStatus.RUNNING
    engineer: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_tokens: int = 0
    total_cost: float = 0.0
    archived: bool = False
    """Added after the base schema shipped; readers must tolerate its absence (§9.4)."""


class PhaseRecord(BaseModel):
    """One phase in the trace.

    Constructed ``FAIL``. A clean exit is what flips it to ``SUCCESS`` — success is earned in the
    type system as well as in the database (§1.3, §9.3).
    """

    model_config = ConfigDict(extra="forbid")

    phase_id: str
    f_id: str
    seq: int
    name: str
    kind: PhaseKind
    owner: str
    description: str
    status: PhaseStatus = PhaseStatus.FAIL
    attempt: int = 1
    retries: int = 0
    error: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    f_id: str
    phase_id: str = ""
    parent_id: str = ""
    type: EventType
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    tokens: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    """Only ``tool_call`` spans real elapsed time; for every other type these are the same instant."""


class EnvelopeRecord(BaseModel):
    """A persisted envelope — including the invalid ones (§5.7).

    Every failed parse attempt is stored with ``valid=False`` so the trace shows what the agent
    actually said, not merely that it was wrong.
    """

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    f_id: str
    phase_id: str
    agent: str
    output_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    valid: bool = False
    attempt: int = 1
    created_at: datetime | None = None


class GateResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    f_id: str
    phase_id: str
    attempt: int
    gate: str
    passed: bool
    violations: list[str] = Field(default_factory=list)
    checks: list[GateCheck] = Field(default_factory=list)
    created_at: datetime | None = None


class ProcessRecord(BaseModel):
    """A spawned child process.

    ``ended_at is None`` means *believed alive*. ``command`` is stored so a recycled pid is never
    killed by mistake (§9.3).
    """

    model_config = ConfigDict(extra="forbid")

    f_id: str
    kind: str
    name: str
    pid: int
    command: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def believed_alive(self) -> bool:
        return self.ended_at is None


class AgentSessionRecord(BaseModel):
    """One agent's resumable backend session within a run (§5.8).

    Changing an agent's **model** invalidates this and mints a new ``session_id``; changing
    **thinking** does not.
    """

    model_config = ConfigDict(extra="forbid")

    f_id: str
    agent: str
    coding_agent: str
    model: str
    color: str = ""
    session_id: str = ""
    context_tokens: int = 0
    context_window: int = 0
    created_at: datetime | None = None
    last_used_at: datetime | None = None
