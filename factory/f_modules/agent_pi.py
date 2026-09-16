"""The Pi backend.

Written against Pi **0.83.0** as it actually behaves, not against §8.6's description of an earlier
version — the differences are recorded on the resolved contract ticket and reproduced here where
they change the code.

The invocation::

    pi --mode json --provider P --model M --thinking T
       --session-id S --session-dir D --tools a,b --system-prompt TEXT PROMPT

``-p`` is redundant: ``resolveAppMode`` checks ``--mode json`` before ``--print``. There is no
``pi run`` subcommand and no ``--allowed-tools`` flag.

Three things here exist because of §8.7's hard-won lessons, and each is cheaper to obey than to
rediscover:

- ``stdin=DEVNULL``. The prompt travels in argv, so the child never needs stdin — but Pi's
  ``readPipedStdin()`` returns immediately only when stdin is a TTY, and otherwise **awaits ``end``
  before the run starts**. An inherited, non-TTY, never-closed stdin therefore hangs forever: 0% CPU,
  an empty raw output file, and the harness blocked on a read loop with nothing to read.
- ``on_spawn(pid)`` / ``on_exit(pid)`` bracket the child, because a hung agent emits no events at
  all — exactly when you need its pid — and ``ps`` cannot tell you which run it belongs to.
- Every raw line is flushed to disk as it is read, not at process exit.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from f_modules.data_types import AgentRequest, AgentResult, ToolCallRecord, Usage
from f_modules.utils import operator_env, utc_now

PI_BINARY = os.environ.get("PI_PATH", "pi")

_CATALOG_PATH = Path.home() / ".pi" / "agent" / "models-store.json"

# Pi's built-in tool names are `read, bash, edit, write, grep, find, ls` — identical to the
# harness's canonical set, so translation is the identity function for this backend. The seam still
# earns its place for the next backend, and unknown names (extension and MCP tool ids) pass through
# unchanged either way (§8.4).
_TOOL_NAMES: dict[str, str] = {}

# Which argument to show in a tool call's one-line label. Falls back to the first short string arg.
_LABEL_ARG = {"bash": "command", "read": "path", "write": "path", "edit": "path",
              "ls": "path", "find": "pattern", "grep": "pattern"}


def translate_tools(names: list[str]) -> list[str]:
    """Canonical names to Pi's spelling. Unknown names pass through unchanged (§8.4)."""
    return [_TOOL_NAMES.get(name, name) for name in names]


# ---------------------------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------------------------


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to ``(provider, model_id)``, or raise.

    §3.2 requires this to resolve **unambiguously** before anything spawns, so an ambiguous pattern
    is an error naming every candidate rather than a silent pick of the first.
    """
    search = pattern.split("/", 1)[-1]
    proc = subprocess.run(
        [PI_BINARY, "--list-models", search],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=operator_env(),
        timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"could not list models for {pattern!r}: {proc.stderr.strip()}")

    candidates: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "provider":
            continue
        candidates.append((parts[0], parts[1]))

    if "/" in pattern:
        wanted_provider, wanted_model = pattern.split("/", 1)
        candidates = [c for c in candidates if c == (wanted_provider, wanted_model)]
    else:
        candidates = [c for c in candidates if c[1] == pattern] or candidates

    if not candidates:
        raise ValueError(f"model pattern {pattern!r} matched nothing in Pi's catalog")
    if len(candidates) > 1:
        listed = ", ".join(f"{p}/{m}" for p, m in candidates)
        raise ValueError(f"model pattern {pattern!r} is ambiguous: {listed}")
    return candidates[0]


def context_window(provider: str, model_id: str) -> int:
    """The model's context window, or ``0`` when unknown (§8.1).

    Read from Pi's cached catalog rather than from ``--list-models``, whose ``context`` column is a
    *display* string: ``1M`` and ``128K`` happen to be exact, but the catalog also holds ``1050000``
    and a rounded ``1.05M`` would be silently wrong. A wrong window mis-renders occupancy forever;
    ``0`` means unknown, which a UI can say out loud.
    """
    if not _CATALOG_PATH.exists():
        return 0
    try:
        catalog = json.loads(_CATALOG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return 0

    for entry in _iter_catalog_models(catalog):
        if entry.get("provider") == provider and entry.get("id") == model_id:
            return int(entry.get("contextWindow") or 0)
    return 0


def _iter_catalog_models(catalog: Any):
    """Walk the cached catalog without assuming its exact nesting, which is Pi's private shape."""
    stack = [catalog]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "id" in node and "contextWindow" in node:
                yield node
            else:
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


# ---------------------------------------------------------------------------------------------
# Tool call tracking (§8.9)
# ---------------------------------------------------------------------------------------------


class PiToolTracker:
    """Folds ``tool_execution_start``/``update``/``end`` into one record per real call.

    Pi's tool events carry **no timestamps at all** — verified empirically, the keys are exactly
    ``type/toolCallId/toolName/args`` and ``type/toolCallId/toolName/result/isError``. §8.9 wants
    each call's real span in dedicated fields, so the span is stamped here from the harness's own
    clock at the moment each line is read.

    That is only correct while the adapter is streaming. Buffer the output and every span collapses
    to the moment the process exited — which is why §8.5 is a correctness rule, not just a liveness
    one.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict[str, Any]] = {}

    def observe(self, event: dict[str, Any]) -> ToolCallRecord | None:
        kind = event.get("type")
        call_id = event.get("toolCallId")
        if not call_id:
            return None

        if kind == "tool_execution_start":
            self._open[call_id] = {
                "name": event.get("toolName", ""),
                "args": event.get("args") or {},
                "started_at": utc_now(),
            }
            return None

        if kind == "tool_execution_end":
            started = self._open.pop(call_id, None)
            name = event.get("toolName", "") or (started or {}).get("name", "")
            args = (started or {}).get("args") or event.get("args") or {}
            return ToolCallRecord(
                call_id=call_id,
                name=name,
                arguments=args if isinstance(args, dict) else {"value": args},
                ok=not bool(event.get("isError")),
                result_snippet=_snippet(event.get("result")),
                label=_label(name, args),
                started_at=(started or {}).get("started_at") or utc_now(),
                ended_at=utc_now(),
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
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            result = "\n".join(t for t in texts if t)
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return text[:limit]


def make_tool_tracker() -> PiToolTracker:
    return PiToolTracker()


# ---------------------------------------------------------------------------------------------
# Running a turn
# ---------------------------------------------------------------------------------------------


def build_argv(request: AgentRequest) -> list[str]:
    argv = [PI_BINARY, "--mode", "json"]
    if request.model:
        provider, model_id = (request.model.split("/", 1) + [""])[:2] if "/" in request.model \
            else ("", request.model)
        if provider:
            argv += ["--provider", provider]
        argv += ["--model", model_id or request.model]
    if request.thinking:
        argv += ["--thinking", request.thinking.value]
    if request.session_id:
        argv += ["--session-id", request.session_id]
    if request.session_dir:
        argv += ["--session-dir", request.session_dir]
    if request.tools:
        # grep, find and ls are OFF by default in Pi, so an allowlist must name them explicitly.
        argv += ["--tools", ",".join(translate_tools(request.tools))]
    for extension in request.extensions:
        argv += ["-e", extension]
    if request.system_prompt:
        argv += ["--system-prompt", request.system_prompt]
    argv.append(request.prompt)
    return argv


def run(
    request: AgentRequest,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> AgentResult:
    """Spawn one Pi turn, streaming its events as they are produced (§8.5)."""
    argv = build_argv(request)
    raw_path = Path(request.raw_output_path) if request.raw_output_path else None
    if raw_path:
        raw_path.parent.mkdir(parents=True, exist_ok=True)

    # stderr goes to its own file rather than a pipe. Reading it only after draining stdout would
    # deadlock the moment Pi wrote more than a pipe buffer's worth, and merging it into stdout would
    # corrupt the NDJSON stream with prose. Pi does use stderr for non-fatal warnings — a missing
    # --session-id warns there and proceeds — so it is kept, not discarded.
    stderr_path = raw_path.with_suffix(".stderr.log") if raw_path else None
    stderr_handle = stderr_path.open("a", encoding="utf-8") if stderr_path else subprocess.DEVNULL

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,  # §8.7 — not hygiene; Pi waits forever otherwise
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
    raw = raw_path.open("a", encoding="utf-8") if raw_path else None
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
        if raw:
            raw.close()
        if stderr_handle is not subprocess.DEVNULL:
            stderr_handle.close()
        if on_exit:
            on_exit(process.pid)

    return AgentResult(
        text=state.text,
        returncode=returncode,
        session_id=state.session_id or request.session_id,
        tokens=state.spend.total,
        cost=state.spend.cost,
        usage=state.spend,
        context_tokens=state.occupancy,
        context_window=0,  # filled in by the caller, which knows the resolved provider/model
    )


class _TurnState:
    """Accumulates what one turn produced, as its events stream past.

    §8.8's two numbers are computed differently on purpose:

    - **Spend** accumulates across *every* send, because retries cost money. Summed.
    - **Occupancy** is the window's state after the last *valid* turn. Overwritten, never summed —
      and never overwritten by an aborted turn, whose usage is untrustworthy. Pi's ``agent_end``
      carries ``willRetry``, which is how an aborted turn announces itself.

    Reasoning tokens are reported nested under output and never added: Pi's own type documents
    ``reasoning`` as "a subset of ``output``".
    """

    def __init__(self) -> None:
        self.session_id = ""
        self.text = ""
        self.occupancy = 0
        self._aborted = False
        self._input = self._output = self._cache_read = self._cache_write = 0
        self._reasoning = self._total = 0
        self._cost = 0.0
        self._input_cost = self._output_cost = 0.0
        self._cache_read_cost = self._cache_write_cost = 0.0

    @property
    def spend(self) -> Usage:
        return Usage(
            input=self._input,
            output=self._output,
            cache_read=self._cache_read,
            cache_write=self._cache_write,
            reasoning=self._reasoning,
            total=self._total,
            cost=self._cost,
            input_cost=self._input_cost,
            output_cost=self._output_cost,
            cache_read_cost=self._cache_read_cost,
            cache_write_cost=self._cache_write_cost,
        )

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")

        if kind == "session":
            self.session_id = event.get("id", "") or self.session_id
            return

        if kind == "turn_start":
            self._aborted = False
            return

        if kind == "agent_end" and event.get("willRetry"):
            # This turn is being thrown away; its usage must not become the occupancy reading.
            self._aborted = True
            return

        if kind != "message_end":
            return

        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return

        text = _message_text(message)
        if text:
            self.text = text  # the last assistant message is the final response

        usage = message.get("usage") or {}
        if not usage:
            return

        self._input += int(usage.get("input") or 0)
        self._output += int(usage.get("output") or 0)
        self._cache_read += int(usage.get("cacheRead") or 0)
        self._cache_write += int(usage.get("cacheWrite") or 0)
        self._reasoning += int(usage.get("reasoning") or 0)
        self._total += int(usage.get("totalTokens") or 0)
        cost = usage.get("cost") or {}
        self._cost += float(cost.get("total") or 0.0)
        self._input_cost += float(cost.get("input") or 0.0)
        self._output_cost += float(cost.get("output") or 0.0)
        self._cache_read_cost += float(cost.get("cacheRead") or 0.0)
        self._cache_write_cost += float(cost.get("cacheWrite") or 0.0)

        if not self._aborted:
            self.occupancy = int(usage.get("totalTokens") or 0)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""
