"""Config load, merge and validation.

Agent *execution* and the parse-and-gate pipeline are the other half of this module and arrive with
their own ticket. What is here is everything that must happen **before anything spawns** (§3.2).

The economics of that ordering: a misnamed agent discovered in phase 4 has already cost four agent
invocations. Validation is free; spawning is not. So every problem is found in one pass and reported
together, rather than one per run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from f_modules import permissions, prompts
from f_modules.agent_base import AdapterNotFound, load_adapter
from f_modules.data_types import (
    AgentCall,
    AgentRequest,
    AgentResult,
    AgentSessionRecord,
    AgentSpec,
    BuildOutput,
    Defaults,
    DocumentOutput,
    EnvelopeBase,
    FactoryConfig,
    GenericOutput,
    PlanOutput,
    EnvelopeRecord,
    EventType,
    GateResultRecord,
    ProcessRecord,
    ResolvedAgent,
    ReviewOutput,
    ScoutOutput,
)
from f_modules.runner import PhaseFailed
from f_modules.session import AgentMap, session_paths
from f_modules.utils import new_id

DEFAULT_CONFIG = "factory/f_config/sssf.config.yaml"

AGENT_OUTPUT_TYPES: dict[str, type[EnvelopeBase]] = {
    "planner": PlanOutput,
    "builder": BuildOutput,
    "scout": ScoutOutput,
    "reviewer": ReviewOutput,
    "documenter": DocumentOutput,
}
"""The conventional output type for each starter agent, with ``GenericOutput`` as the fallback.

This is a **convenience for scaffolding**, not a contract. §3.3 keeps output types out of config on
purpose: config defines identity, the call site defines use, and the same agent can return different
types in different FWs. An FW is free to ignore this map entirely.
"""


class ConfigError(Exception):
    """Roster problems, all of them, in one message."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"{len(problems)} configuration problem(s):\n  - {joined}")


def load_config(path: str | Path = DEFAULT_CONFIG) -> FactoryConfig:
    source = Path(path)
    if not source.exists():
        raise ConfigError([f"{source}: no roster found"])
    return FactoryConfig.model_validate(yaml.safe_load(source.read_text()) or {})


def merge(spec: AgentSpec, defaults: Defaults) -> ResolvedAgent:
    """Merge one agent entry over ``defaults``, **key by key** (§3.2).

    Every inheritable field is named explicitly. Constructing a :class:`ResolvedAgent` — where none
    of these are optional — is what proves the merge covered them: a forgotten key is a construction
    error rather than a default quietly winning.

    ``writes`` is the exception that proves the rule. It is **not** inherited from defaults, because
    ``None`` (unrestricted) and ``[]`` (repository-read-only) are opposites, and a default would
    silently pick one of them for an agent that named neither.
    """
    return ResolvedAgent(
        name=spec.name,
        purpose=spec.purpose,
        prompt_engineering=spec.prompt_engineering,
        coding_agent=spec.coding_agent if spec.coding_agent is not None else defaults.coding_agent,
        model=spec.model if spec.model is not None else defaults.model,
        thinking=spec.thinking if spec.thinking is not None else defaults.thinking,
        color=spec.color if spec.color is not None else defaults.color,
        harness_engineering=(
            spec.harness_engineering
            if spec.harness_engineering is not None
            else list(defaults.harness_engineering)
        ),
        tools=spec.tools if spec.tools is not None else list(defaults.tools),
        writes=spec.writes,
    )


def resolve(config: FactoryConfig, name: str) -> ResolvedAgent:
    for spec in config.agents:
        if spec.name == name:
            return merge(spec, config.defaults)
    raise ConfigError([f"{name}: no agent with that name in the roster"])


def roster(config: FactoryConfig) -> dict[str, ResolvedAgent]:
    return {spec.name: merge(spec, config.defaults) for spec in config.agents}


def validate(
    config: FactoryConfig,
    required_agents: list[str],
    check_models: bool = True,
) -> dict[str, ResolvedAgent]:
    """Check everything an FW needs before it spawns anything (§3.2).

    1. every required name resolves to a roster entry,
    2. the ``coding_agent`` value has a loadable adapter,
    3. both prompt files exist on disk,
    4. the model pattern resolves unambiguously against the backend's catalog.

    Failure is a **single aggregated error listing all problems**, not the first one. Returning after
    the first would make fixing a roster an N-run guessing game.

    ``check_models=False`` skips step 4 only, which is the one that shells out to the backend.
    """
    problems: list[str] = []
    known = {spec.name: spec for spec in config.agents}
    resolved: dict[str, ResolvedAgent] = {}

    for name in required_agents:
        spec = known.get(name)
        if spec is None:
            available = ", ".join(sorted(known)) or "none"
            problems.append(f"{name}: not in the roster (available: {available})")
            continue

        agent = merge(spec, config.defaults)
        resolved[name] = agent

        adapter = None
        try:
            adapter = load_adapter(agent.coding_agent)
        except AdapterNotFound as exc:
            problems.append(f"{name}: {exc}")

        for role, path in (
            ("system", agent.prompt_engineering.system),
            ("user", agent.prompt_engineering.user),
        ):
            if not Path(path).exists():
                problems.append(f"{name}: {role} prompt not found at {path}")

        if check_models and adapter is not None:
            try:
                adapter.resolve_model(agent.model)
            except Exception as exc:  # the adapter decides what "unambiguous" means
                problems.append(f"{name}: {exc}")

    if problems:
        raise ConfigError(problems)
    return resolved


def output_type_for(name: str) -> type[EnvelopeBase]:
    return AGENT_OUTPUT_TYPES.get(name, GenericOutput)


# ---------------------------------------------------------------------------------------------
# Execution: parse, gate, repair (§5.7)
# ---------------------------------------------------------------------------------------------

MAX_JSON_ATTEMPTS = 2
"""The JSON-repair budget — a fixed constant, and **separate** from the gate budget (§10.4).

Three bounded mechanisms exist and must never be conflated: JSON retries repair a malformed response
object; gate retries (``PhaseParams.retries``) repair valid JSON making unacceptable claims; a fix
loop (``MAX_FIX_LOOPS``) repairs actual code that does not work, and belongs to the FW.
"""

CORRECTION_TEMPLATE = """Your previous response failed validation:
{violations}

Fix these problems, then re-emit ONLY your Report JSON."""


def correction_prompt(violations: list[str]) -> str:
    """Mechanical and specific: the violation notes verbatim, and nothing else.

    The notes are the gate's own evidence text (§6.2), so what a human reads in the trace and what
    the agent is told to fix are the same words — written once.
    """
    return CORRECTION_TEMPLATE.format(violations="\n".join(f"- {v}" for v in violations))


def extract_json(text: str) -> tuple[dict | None, str]:
    """Find the response object in an agent's reply.

    Forgiving in **exactly one direction**: bare JSON is what the prompt asks for, but a fenced
    ``json`` block or prose wrapped around the object is tolerated before rejection. Being strict
    here would spend a correction round on formatting rather than on content; being any looser would
    mean guessing which of several objects the agent meant.
    """
    stripped = text.strip()
    try:
        loaded = json.loads(stripped)
        if isinstance(loaded, dict):
            return loaded, ""
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        try:
            loaded = json.loads(fence.group(1))
            if isinstance(loaded, dict):
                return loaded, ""
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            loaded = json.loads(text[start : end + 1])
            if isinstance(loaded, dict):
                return loaded, ""
        except json.JSONDecodeError:
            pass

    return None, "the response contained no JSON object"


def parse_envelope(
    text: str, output_type: type[EnvelopeBase]
) -> tuple[EnvelopeBase | None, list[str], dict]:
    """Parse a reply against its declared type, returning **every** violation."""
    payload, problem = extract_json(text)
    if payload is None:
        return None, [problem], {"raw": text[:4000]}
    try:
        return output_type.model_validate(payload), [], payload
    except ValidationError as exc:
        violations = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        return None, violations, payload


class Executor:
    """Runs one agent turn, then repairs it rather than restarting it.

    Neither budget ever cold-restarts the agent. A response that is 95% right is repaired for the
    price of one correction turn, whereas a restart throws away everything the agent learned and
    pays for the whole discovery again — which is why every re-prompt reuses the **same** backend
    session id.
    """

    def __init__(self, run, roster_: dict[str, ResolvedAgent], cwd: Path | None = None) -> None:
        self.run = run
        self.roster = roster_
        self.cwd = Path(cwd or Path.cwd())
        self.paths = session_paths(run.config, run.f_id)
        self.agent_map = AgentMap(self.paths["agent_map"])
        self._windows: dict[str, int] = {}

    # -- the executor the runner calls ---------------------------------------------------

    def __call__(self, phase, call: AgentCall) -> EnvelopeBase:
        agent = self.roster.get(phase.record.owner)
        if agent is None:
            raise ConfigError([f"{phase.record.owner}: not in the validated roster"])

        adapter = load_adapter(agent.coding_agent)
        tracer = self.run.tracer

        system, user = self._render(agent, call)
        prompts.save_audit_copy(self.paths["session_dir"], agent.name, system, user)

        if call.previous is not None:
            tracer.event(
                EventType.HANDOFF,
                phase_id=phase.phase_id,
                name=agent.name,
                payload={"from": type(call.previous).__name__,
                         "summary": call.previous.summary},
            )

        # One baseline for the whole phase: the first prompt, every JSON repair and every gate
        # correction are all attributed against it (§4.2).
        before = permissions.snapshot(self.cwd)
        session_id = self.agent_map.session_id_for(agent.name, agent.model, agent.coding_agent)

        envelope = self._converse(phase, call, agent, adapter, system, user, session_id)

        # §4.5: a breach is not a gate violation. It cannot be corrected by re-prompting, because
        # the write already happened, so this raises straight through the phase.
        permissions.enforce_or_raise(before, agent, self.run.config.defaults, self.cwd)
        return envelope

    def _render(self, agent: ResolvedAgent, call: AgentCall) -> tuple[str, str]:
        """Render both prompt files (§11.1, §5.6).

        ``{{prompt}}`` takes the per-call override when there is one, and otherwise the engineer's
        original ask — the request that opened the session, not a paraphrase of it.
        """
        system = Path(agent.prompt_engineering.system).read_text()
        user = Path(agent.prompt_engineering.user).read_text()
        ask = call.prompt if call.prompt is not None else self.run.request
        return (
            prompts.render(system, ask, call.previous, str(self.paths["context_handoff"])),
            prompts.render(user, ask, call.previous, str(self.paths["context_handoff"])),
        )

    # -- the repair loop -------------------------------------------------------------------

    def _converse(self, phase, call, agent, adapter, system, user, session_id) -> EnvelopeBase:
        tracer = self.run.tracer
        prompt_text = user
        json_attempts = 0
        gate_attempts = 0
        attempt = 0

        while True:
            attempt += 1
            phase.record.attempt = attempt
            tracer.event(
                EventType.AGENT_START,
                phase_id=phase.phase_id,
                name=agent.name,
                payload={"attempt": attempt, "model": agent.model,
                         "thinking": agent.thinking.value},
            )

            result = self._spawn(phase, agent, adapter, system, prompt_text, session_id)
            window = self._context_window(adapter, agent)
            envelope, violations, payload = parse_envelope(result.text, call.output_type)

            tracer.envelope(
                EnvelopeRecord(
                    envelope_id=new_id("env"),
                    f_id=self.run.f_id,
                    phase_id=phase.phase_id,
                    agent=agent.name,
                    output_type=call.output_type.__name__,
                    payload=payload,
                    valid=envelope is not None,
                    attempt=attempt,
                )
            )

            if envelope is None:
                json_attempts += 1
                self.run.console.say(
                    f"{agent.name}: response did not parse ({json_attempts}/{MAX_JSON_ATTEMPTS})",
                    phase_id=phase.phase_id,
                )
                if json_attempts >= MAX_JSON_ATTEMPTS:
                    raise PhaseFailed(
                        f"{agent.name} never returned valid {call.output_type.__name__} "
                        f"after {json_attempts} attempts: {'; '.join(violations)}"
                    )
                prompt_text = correction_prompt(violations)
                continue  # SAME session id: the context window survives the repair

            if envelope.failed:
                # A structurally valid envelope reporting failure still fails the phase (§5.2).
                raise PhaseFailed(f"{agent.name} reported status=fail: {envelope.summary}")

            gate_violations = self._run_gates(phase, call, envelope, attempt)
            if not gate_violations:
                # §5.8's session layout: the final parsed response, beside the prompts and the
                # raw stream, so the whole turn is readable on disk without opening the database.
                envelope_path = self.paths["session_dir"] / agent.name / "envelope.json"
                envelope_path.parent.mkdir(parents=True, exist_ok=True)
                envelope_path.write_text(envelope.model_dump_json(indent=2) + "\n")

                tracer.event(
                    EventType.AGENT_END,
                    phase_id=phase.phase_id,
                    name=agent.name,
                    tokens=result.usage.total,
                    payload=_agent_end_payload(attempt, envelope, result, window),
                )
                return envelope

            gate_attempts += 1
            if gate_attempts > phase.record.retries:
                raise PhaseFailed(
                    f"{agent.name} failed its gates after {gate_attempts} attempt(s): "
                    + "; ".join(gate_violations)
                )
            prompt_text = correction_prompt(gate_violations)

    # -- one backend turn ------------------------------------------------------------------

    def _spawn(self, phase, agent, adapter, system, prompt_text, session_id) -> AgentResult:  # noqa: D401
        tracer = self.run.tracer
        tracker = adapter.make_tool_tracker()
        process_rows: dict[int, int] = {}

        def on_event(event: dict) -> None:
            record = tracker.observe(event)
            if record is None:
                return
            # One event per real tool call, carrying the span in dedicated fields (§8.9).
            tracer.event(
                EventType.TOOL_CALL,
                phase_id=phase.phase_id,
                name=record.name,
                payload={"call_id": record.call_id, "label": record.label,
                         "arguments": record.arguments, "ok": record.ok,
                         "result": record.result_snippet},
                started_at=record.started_at,
                ended_at=record.ended_at,
            )

        def on_spawn(pid: int) -> None:
            # A hung agent emits no events at all, which is exactly when the pid is needed (§8.7).
            process_rows[pid] = tracer.process_start(
                ProcessRecord(f_id=self.run.f_id, kind="agent", name=agent.name, pid=pid,
                              command=f"{agent.coding_agent} {agent.model}")
            )

        def on_exit(pid: int) -> None:
            row = process_rows.pop(pid, None)
            if row:
                tracer.process_end(row)

        request = AgentRequest(
            prompt=prompt_text,
            system_prompt=system,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            cwd=str(self.cwd),
            tools=agent.tools,
            extensions=agent.harness_engineering,
            session_dir=str(self.paths["session_dir"] / agent.name / "backend"),
            raw_output_path=str(self.paths["session_dir"] / agent.name / "raw_output.jsonl"),
        )
        result = adapter.run(request, on_event, on_spawn, on_exit)

        # Spend sums across every send, because retries cost money. Occupancy is overwritten.
        self.run.record_spend(result.tokens, result.cost)
        tracer.agent_session(
            AgentSessionRecord(
                f_id=self.run.f_id,
                agent=agent.name,
                coding_agent=agent.coding_agent,
                model=agent.model,
                color=agent.color,
                session_id=session_id,
                context_tokens=result.context_tokens,
                context_window=self._context_window(adapter, agent),
            )
        )
        return result

    def _context_window(self, adapter, agent: ResolvedAgent) -> int:
        """Resolved once per agent, not once per turn — resolution shells out to the backend."""
        if agent.name not in self._windows:
            try:
                provider, model_id = adapter.resolve_model(agent.model)
                self._windows[agent.name] = adapter.context_window(provider, model_id)
            except Exception:
                self._windows[agent.name] = 0  # 0 means unknown, and a UI can say so
        return self._windows[agent.name]

    # -- gates ------------------------------------------------------------------------------

    def _run_gates(self, phase, call: AgentCall, envelope: EnvelopeBase, attempt: int) -> list[str]:
        """Run **every** gate and collect **all** violations — never stop at the first."""
        tracer = self.run.tracer
        violations: list[str] = []
        for gate in call.gates:
            report = gate(envelope, self.run)
            tracer.gate_result(
                GateResultRecord(
                    f_id=self.run.f_id,
                    phase_id=phase.phase_id,
                    attempt=attempt,
                    gate=report.gate,
                    passed=report.passed,
                    violations=report.violations,
                    checks=report.checks,
                )
            )
            tracer.event(
                EventType.GATE_PASS if report.passed else EventType.GATE_FAIL,
                phase_id=phase.phase_id,
                name=report.gate,
                payload={"checks": [c.model_dump(mode="json") for c in report.checks]},
            )
            violations.extend(report.violations)
        return violations


def _agent_end_payload(
    attempt: int, envelope: EnvelopeBase, result: AgentResult, window: int
) -> dict:
    """What one agent turn cost, recorded where a reader can find it.

    The trace is the only place this survives: ``sessions.total_tokens`` is the whole run and
    ``agent_sessions`` keeps occupancy, so without this event nobody can say what a *single* turn
    spent — which is exactly the question asked of a phase that was retried.

    ``usage`` carries the per-component **cost** split only when the backend actually reported one.
    Copilot does not, and emitting zeros there would read as "input was free" rather than "not
    reported"; the consumer falls back to the total, which is always real.
    """
    usage = result.usage
    # §8.2 puts `cost` on the result itself; `usage.cost` is the same number where a backend
    # fills both, so the result wins and the usage value is the fallback.
    cost = result.cost or usage.cost
    payload: dict = {
        "attempt": attempt,
        "summary": envelope.summary,
        "cost": cost,
        "context_tokens": result.context_tokens,
        "context_window": window,
        "usage": {
            "input_tokens": usage.input,
            "output_tokens": usage.output,
            "cache_read_tokens": usage.cache_read,
            "cache_write_tokens": usage.cache_write,
            "reasoning_tokens": usage.reasoning,
            "total_tokens": usage.total,
            "total_cost": cost,
        },
    }
    if usage.has_cost_breakdown:
        payload["usage"].update(
            input_cost=usage.input_cost,
            output_cost=usage.output_cost,
            cache_read_cost=usage.cache_read_cost,
            cache_write_cost=usage.cache_write_cost,
        )
    return payload


def attach(run, roster_: dict[str, ResolvedAgent], cwd: Path | None = None) -> Executor:
    """Wire a parse-and-gate executor into a run.

    Called by the FW rather than by ``session.ensure``, so the dependency runs one way: this module
    knows about the runner, and the runner knows nothing about this module.
    """
    executor = Executor(run, roster_, cwd)
    run.agent_executor = executor
    return executor
