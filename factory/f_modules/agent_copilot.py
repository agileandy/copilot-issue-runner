"""The GitHub Copilot CLI backend.

Written against ``copilot`` **1.0.78** as it actually behaves, verified empirically against the
installed binary rather than against its documentation. The invocation::

    copilot --output-format json --allow-all-tools --allow-all-paths --no-ask-user --no-color
            --no-remote --model M --effort E --session-id UUID --available-tools a,b
            [--plugin-dir P] --prompt TEXT

Five differences from Pi (§8.6) shape everything in this module, and each one is load-bearing:

- **There is no ``--system-prompt`` flag.** The system prompt is therefore prepended to the first
  prompt of a session, inside a framed block that names it as standing instructions. Later turns in
  the same session — JSON repairs and gate corrections (§5.7) — omit it, because the session already
  carries it. Whether a session is new is not guessed: it is *asked* of Copilot's own session store,
  so a session that was pruned behind the harness's back is re-seeded instead of running blind.
- **``--session-id`` accepts only a v4-shaped UUID**, and the harness mints 8 hex characters
  (``new_f_id``). The two are bridged by a deterministic derivation, so the same harness id always
  addresses the same Copilot session and ``--session-id`` keeps its create-or-continue behaviour.
- **Usage is not in the event stream.** Every ``assistant.message`` carries ``outputTokens`` and
  nothing else — no input, no cache, no cost. The full per-call breakdown *is* recorded, in
  Copilot's local session store, so §8.8's two numbers are read from there after the turn and the
  stream total is kept only as a degraded fallback.
- **Cost is denominated in AI units (AIU), not dollars.** ``AgentResult.cost`` therefore carries
  AIU for this backend. A currency conversion would be an invention.
- **``--effort`` is fatal on a model that cannot honour it**, rather than ignored. §8.3's ladder is
  advisory, so a roster that is legal by the spec — ``thinking: low`` on ``claude-haiku-4.5``, say —
  would otherwise kill the phase on an argument error. The turn is respawned once without the flag,
  and the model is remembered so the wasted spawn is paid once per process rather than once a turn.

Tool events already carry real timestamps, so unlike Pi the span in each :class:`ToolCallRecord`
comes from the backend's own clock rather than the harness's.

``--no-auto-update`` is deliberately not passed, despite being the obvious flag for an unattended
run: it was measured to suppress the session store's usage rows entirely, which silently zeroes the
accounting above.

§8.7's subprocess lessons apply unchanged and for the same reasons: ``stdin=DEVNULL``, the child
bracketed by ``on_spawn``/``on_exit``, and every raw line flushed as it is read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from f_modules.data_types import AgentRequest, AgentResult, ToolCallRecord, Usage
from f_modules.utils import operator_env, utc_now

COPILOT_BINARY = os.environ.get("COPILOT_PATH", "copilot")

PROVIDER = "github-copilot"
"""Copilot serves every model in its catalog itself, so the provider half of §8.1's
``(provider, model_id)`` pair is a constant rather than a lookup."""

_STATE_DIR = Path(os.environ.get("COPILOT_STATE_DIR", str(Path.home() / ".copilot")))

_SESSION_STORE = Path(
    os.environ.get("COPILOT_SESSION_STORE", str(_STATE_DIR / "session-store.db"))
)
"""Copilot's own SQLite store, opened **read-only**. It is the only place a turn's input, cache and
AIU cost are recorded (see the module docstring)."""

_CONTEXT_WINDOWS = Path(
    os.environ.get("COPILOT_CONTEXT_WINDOWS", str(_STATE_DIR / "context-windows.json"))
)
"""Optional operator-supplied ``{model_id: window}`` map. The CLI does not publish context windows
anywhere a subprocess can read them, and §8.1 says ``0`` when unknown — so a wrong number is never
guessed, and an operator who knows the real one can supply it."""

_SESSION_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# Canonical names to Copilot's spelling (§8.4). `find` and `ls` both collapse onto `glob`, which is
# why translation deduplicates; unknown names — extension and MCP tool ids — pass through unchanged.
_TOOL_NAMES: dict[str, str] = {
    "read": "view",
    "write": "create",
    "find": "glob",
    "ls": "glob",
}

# `off` is the one rung of §8.3's ladder Copilot spells differently; the rest match exactly.
_EFFORT: dict[str, str] = {"off": "none"}

_EFFORT_UNSUPPORTED = "does not support reasoning effort"

_NO_EFFORT: set[str] = set()
"""Models observed to reject ``--effort`` outright. Learned at runtime rather than hard-coded,
because the catalog changes faster than any list in this file could."""

# Which argument to show in a tool call's one-line label. Falls back to the first short string arg.
_LABEL_ARG = {"bash": "command", "view": "path", "create": "path", "edit": "path",
              "grep": "pattern", "glob": "pattern", "str_replace": "path"}

SYSTEM_PROMPT_FRAME = """<standing-instructions>
{system_prompt}
</standing-instructions>

The block above is your system prompt for this entire session. Follow it on every turn, including
later turns in this session, even though it is not repeated.

{prompt}"""


def translate_tools(names: list[str]) -> list[str]:
    """Canonical names to Copilot's spelling, order preserved and duplicates removed."""
    out: list[str] = []
    for name in names:
        translated = _TOOL_NAMES.get(name, name)
        if translated not in out:
            out.append(translated)
    return out


def session_uuid(session_id: str) -> str:
    """Derive the v4-shaped UUID that addresses this harness session inside Copilot.

    ``--session-id`` is create-or-continue only for a UUID it accepts, and it rejects anything that
    is not shaped like a v4 — including the harness's 8 hex characters, and including a plain v5
    digest. The digest is therefore stamped with v4's version and variant bits. The mapping is pure,
    so the same harness id resumes the same context window on every send (§5.7).
    """
    digest = bytearray(uuid.uuid5(_SESSION_NAMESPACE, session_id).bytes)
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


# ---------------------------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------------------------


def _catalog() -> list[str]:
    """Every model id the installed binary will accept.

    Copilot has no ``--list-models``, but its generated shell completion enumerates the choices for
    ``--model`` — so the catalog still comes from the binary itself and cannot drift from it.
    ``COPILOT_MODELS`` overrides that for a BYOK provider, whose ids the completion script does not
    know about.
    """
    override = os.environ.get("COPILOT_MODELS", "")
    if override.strip():
        return [name.strip() for name in override.split(",") if name.strip()]

    proc = subprocess.run(
        [COPILOT_BINARY, "completion", "bash"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=operator_env(),
        timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"could not read Copilot's model catalog: {proc.stderr.strip()}")

    match = re.search(r"--model\)\s.*?compgen -W '([^']*)'", proc.stdout, re.DOTALL)
    if not match:
        raise ValueError(
            "could not read Copilot's model catalog: its completion script no longer "
            "enumerates --model (set COPILOT_MODELS to override)"
        )
    return match.group(1).split()


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to ``(provider, model_id)``, or raise.

    §3.2 requires this to resolve **unambiguously** before anything spawns, so an ambiguous pattern
    is an error naming every candidate rather than a silent pick of the first. A ``provider/model``
    pattern is accepted so one roster can name the same model for Pi and for Copilot; any other
    provider is rejected, because Copilot would silently ignore it.
    """
    wanted = pattern
    if "/" in pattern:
        provider, wanted = pattern.split("/", 1)
        if provider not in (PROVIDER, "copilot"):
            raise ValueError(
                f"model pattern {pattern!r} names provider {provider!r}, "
                f"but the copilot backend only serves {PROVIDER!r}"
            )

    catalog = _catalog()
    if wanted in catalog:
        return PROVIDER, wanted

    candidates = [name for name in catalog if wanted in name]
    if not candidates:
        raise ValueError(f"model pattern {pattern!r} matched nothing in Copilot's catalog")
    if len(candidates) > 1:
        raise ValueError(f"model pattern {pattern!r} is ambiguous: {', '.join(candidates)}")
    return PROVIDER, candidates[0]


def context_window(provider: str, model_id: str) -> int:
    """The model's context window, or ``0`` when unknown (§8.1).

    Copilot publishes context windows to its status line and nowhere a subprocess can read them, so
    unknown is the honest answer for every model an operator has not declared in
    ``~/.copilot/context-windows.json``. A wrong window mis-renders occupancy forever; ``0`` means
    unknown, which a UI can say out loud.
    """
    if provider not in (PROVIDER, "copilot", ""):
        return 0
    if not _CONTEXT_WINDOWS.exists():
        return 0
    try:
        windows = json.loads(_CONTEXT_WINDOWS.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(windows, dict):
        return 0
    try:
        return int(windows.get(model_id) or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------
# Tool call tracking (§8.9)
# ---------------------------------------------------------------------------------------------


def _stamp(event: dict[str, Any]) -> datetime:
    """The backend's own timestamp for an event, falling back to the harness's clock."""
    raw = event.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return utc_now()


class CopilotToolTracker:
    """Folds ``tool.execution_start``/``partial_result``/``complete`` into one record per real call.

    Copilot timestamps every event, so the span is the backend's own rather than the harness's
    reading clock — but §8.5 still applies, because ``tool.execution_start`` carries the arguments
    and ``tool.execution_complete`` does not: fold them while streaming or lose them.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict[str, Any]] = {}

    def observe(self, event: dict[str, Any]) -> ToolCallRecord | None:
        kind = event.get("type")
        data = event.get("data") or {}
        call_id = data.get("toolCallId")
        if not call_id:
            return None

        if kind == "tool.execution_start":
            self._open[call_id] = {
                "name": data.get("toolName", ""),
                "args": data.get("arguments") or {},
                "started_at": _stamp(event),
            }
            return None

        if kind == "tool.execution_complete":
            started = self._open.pop(call_id, None)
            name = data.get("toolName", "") or (started or {}).get("name", "")
            args = (started or {}).get("args") or data.get("arguments") or {}
            return ToolCallRecord(
                call_id=call_id,
                name=name,
                arguments=args if isinstance(args, dict) else {"value": args},
                # `success` is absent on some tool results; a completed call is ok unless it says
                # otherwise, which is the only reading that does not invent failures.
                ok=bool(data.get("success", True)),
                result_snippet=_snippet(data.get("result")),
                label=_label(name, args),
                started_at=(started or {}).get("started_at") or _stamp(event),
                ended_at=_stamp(event),
            )
        return None


def _label(name: str, args: Any) -> str:
    """A one-line human label, e.g. ``bash: ls -la src``."""
    if not isinstance(args, dict):
        return name
    key = _LABEL_ARG.get(name)
    value = args.get(key) if key else None
    if value is None:
        value = next((v for v in args.values() if isinstance(v, str)), "")
    text = str(value).replace("\n", " ").strip()
    return f"{name}: {text[:80]}" if text else name


def _snippet(result: Any, limit: int = 300) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            result = content
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            result = "\n".join(t for t in texts if t)
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return text[:limit]


def make_tool_tracker() -> CopilotToolTracker:
    return CopilotToolTracker()


# ---------------------------------------------------------------------------------------------
# Running a turn
# ---------------------------------------------------------------------------------------------


def session_exists(session_uuid_: str) -> bool:
    """Whether Copilot already holds this session, and so already holds the system prompt.

    Asked of the store rather than remembered in a marker file: a marker can outlive the session it
    describes, and an agent resumed without its system prompt is an agent without its identity or
    its output contract — which surfaces as unparseable JSON several turns later.
    """
    if not _SESSION_STORE.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{_SESSION_STORE}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_uuid_,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def compose_prompt(request: AgentRequest, fresh: bool) -> str:
    """The prompt text as Copilot will receive it — system prompt included on a fresh session."""
    if not fresh or not request.system_prompt:
        return request.prompt
    return SYSTEM_PROMPT_FRAME.format(
        system_prompt=request.system_prompt.strip(), prompt=request.prompt
    )


def build_argv(request: AgentRequest, fresh: bool = True, effort: bool = True) -> list[str]:
    argv = [
        COPILOT_BINARY,
        "--output-format", "json",
        # Non-interactive mode cannot prompt for approval, so an un-approved tool is a hang.
        # The boundary that matters is enforced after the fact against the real tree (§4.1).
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
        "--no-color",
        # An unattended agent must not accept remote control from GitHub web or mobile.
        #
        # `--no-auto-update` is deliberately ABSENT despite being the obvious companion: it was
        # measured to suppress the session store's usage rows entirely, which silently zeroes §8.8's
        # accounting for every turn. Wrong numbers are worse than an occasional self-update.
        "--no-remote",
    ]
    model = request.model.split("/", 1)[-1] if request.model else ""
    if model:
        argv += ["--model", model]
    if effort and request.thinking and model not in _NO_EFFORT:
        argv += ["--effort", _EFFORT.get(request.thinking.value, request.thinking.value)]
    if request.session_id:
        argv += ["--session-id", session_uuid(request.session_id)]
    if request.tools:
        argv += ["--available-tools", ",".join(translate_tools(request.tools))]
    for extension in request.extensions:
        argv += ["--plugin-dir", extension]
    argv += ["--prompt", compose_prompt(request, fresh)]
    return argv


def run(
    request: AgentRequest,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> AgentResult:
    """Spawn one Copilot turn, streaming its events as they are produced (§8.5)."""
    backend_session = session_uuid(request.session_id) if request.session_id else ""
    fresh = not (backend_session and session_exists(backend_session))

    raw_path = Path(request.raw_output_path) if request.raw_output_path else None
    if raw_path:
        raw_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = utc_now()
    returncode, state, stderr_tail = _stream(
        build_argv(request, fresh=fresh), request, raw_path, on_event, on_spawn, on_exit
    )

    if returncode != 0 and _EFFORT_UNSUPPORTED in stderr_tail:
        # §8.3's ladder is advisory, but Copilot treats an effort it cannot honour as a fatal
        # argument error rather than ignoring it — so a roster that is legal by the spec would kill
        # the phase. The model is remembered, so the wasted spawn is paid once per process.
        _NO_EFFORT.add(request.model.split("/", 1)[-1])
        returncode, state, stderr_tail = _stream(
            build_argv(request, fresh=fresh, effort=False),
            request, raw_path, on_event, on_spawn, on_exit,
        )

    session_id = state.session_id or backend_session
    spend, occupancy = read_usage(session_id, started_at)
    if spend.total == 0 and state.output_tokens:
        # Degraded but honest: the stream reports output tokens and nothing else, so a turn whose
        # usage rows are unreadable still accounts for what it can rather than reporting zero.
        spend = Usage(output=state.output_tokens, total=state.output_tokens)

    return AgentResult(
        text=state.text,
        returncode=returncode,
        session_id=session_id,
        tokens=spend.total,
        cost=spend.cost,
        usage=spend,
        context_tokens=occupancy,
        context_window=0,  # filled in by the caller, which knows the resolved provider/model
    )


def _stream(
    argv: list[str],
    request: AgentRequest,
    raw_path: Path | None,
    on_event: Callable[[dict[str, Any]], None] | None,
    on_spawn: Callable[[int], None] | None,
    on_exit: Callable[[int], None] | None,
) -> tuple[int, "_TurnState", str]:
    """One child process, tailed line by line, returning its exit code, state and stderr tail.

    stderr goes to its own file rather than a pipe, for the same two reasons as Pi: reading it only
    after draining stdout would deadlock past one pipe buffer, and merging it into stdout would
    corrupt the JSONL stream with prose. Copilot does use it — settings warnings land there, and so
    does the one fatal argument error this adapter has to recognise.
    """
    with ExitStack() as stack:
        if raw_path:
            stderr_path = raw_path.with_suffix(".stderr.log")
            raw = stack.enter_context(raw_path.open("a", encoding="utf-8"))
        else:
            stderr_path = Path(stack.enter_context(TemporaryDirectory())) / "stderr.log"
            raw = None
        stderr_handle = stack.enter_context(stderr_path.open("a", encoding="utf-8"))

        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,  # §8.7 — the prompt is in argv; the child never reads stdin
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            text=True,
            bufsize=1,  # line buffered: events must be forwarded while the agent still works
            cwd=request.cwd or None,
            env=operator_env(),
        )
        if on_spawn:
            on_spawn(process.pid)

        state = _TurnState()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                if raw:
                    raw.write(line + "\n")
                    raw.flush()  # on disk as it happens, not at process exit
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                state.observe(event)
                if on_event:
                    on_event(event)
            returncode = process.wait()
        finally:
            if on_exit:
                on_exit(process.pid)

        stderr_handle.flush()
        try:
            tail = stderr_path.read_text(encoding="utf-8")[-4000:]
        except OSError:
            tail = ""
    return returncode, state, tail


class _TurnState:
    """Accumulates what one turn produced, as its events stream past.

    Deliberately thin compared with Pi's: Copilot's stream carries the reply and the session id but
    not the accounting, so §8.8's two numbers are read from the store afterwards by
    :func:`read_usage`. ``outputTokens`` is kept only as the fallback for an unreadable store.
    """

    def __init__(self) -> None:
        self.session_id = ""
        self.text = ""
        self.output_tokens = 0

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")

        if kind == "result":
            self.session_id = event.get("sessionId", "") or self.session_id
            return

        if kind != "assistant.message":
            return

        data = event.get("data") or {}
        text = data.get("content")
        if isinstance(text, str) and text.strip():
            self.text = text  # the last assistant message is the final response
        self.output_tokens += int(data.get("outputTokens") or 0)


def read_usage(session_id: str, since: datetime) -> tuple[Usage, int]:
    """§8.8's two numbers, read from Copilot's store: cumulative spend, and context occupancy.

    Spend sums **every** model call the turn made, sub-agents included, because they cost money.
    Occupancy is read from the last main-thread call only — a sub-agent has its own window, and a
    call that did not finish cleanly is untrustworthy and must not overwrite a good reading.

    ``cost`` is in **AI units**, not dollars; Copilot bills in AIU and a conversion would be an
    invention. ``since`` bounds the rows to this turn, so a resumed session does not re-count the
    spend of every turn before it.
    """
    empty = Usage()
    if not session_id or not _SESSION_STORE.exists():
        return empty, 0
    try:
        conn = sqlite3.connect(f"file:{_SESSION_STORE}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return empty, 0

    try:
        rows = conn.execute(
            "SELECT agent_id, input_tokens, output_tokens, cache_read_tokens, "
            "       cache_write_tokens, reasoning_tokens, total_nano_aiu, finish_reason "
            "FROM assistant_usage_events "
            "WHERE session_id = ? AND created_at >= ? "
            "ORDER BY id",
            (session_id, _iso_ms(since)),
        ).fetchall()
    except sqlite3.Error:
        return empty, 0
    finally:
        conn.close()

    totals = dict.fromkeys(
        ("input", "output", "cache_read", "cache_write", "reasoning", "total"), 0
    )
    cost = 0.0
    occupancy = 0
    for agent_id, prompt, output, cache_read, cache_write, reasoning, nano_aiu, finish in rows:
        prompt = int(prompt or 0)
        output = int(output or 0)
        cache_read = int(cache_read or 0)
        cache_write = int(cache_write or 0)

        # `input_tokens` is the whole prompt, cached parts included. §8.8 wants the four components
        # to be disjoint and to sum to the total, so the cached parts are subtracted back out.
        totals["input"] += max(prompt - cache_read - cache_write, 0)
        totals["output"] += output
        totals["cache_read"] += cache_read
        totals["cache_write"] += cache_write
        totals["reasoning"] += int(reasoning or 0)  # nested under output, never added (§8.8)
        totals["total"] += prompt + output
        cost += float(nano_aiu or 0) / 1e9  # AI units, not dollars

        if agent_id is None and finish != "error":
            occupancy = prompt + output

    return Usage(**totals, cost=cost), occupancy


def _iso_ms(moment: datetime) -> str:
    """Copilot's ``created_at`` spelling, so a string comparison in SQL orders correctly."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
