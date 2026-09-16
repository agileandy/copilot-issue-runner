"""The adapter protocol and lazy adapter lookup.

Every coding-agent backend exposes exactly four callables (§8.1)::

    run(request, on_event, on_spawn, on_exit) -> AgentResult
    resolve_model(pattern)                    -> (provider, model_id)
    context_window(provider, model_id)        -> int          # 0 when unknown
    make_tool_tracker()                       -> ToolCallTracker

This is the main seam in the system (§14.2): everything above it is untouched when a backend
changes. Two properties keep it honest:

**Structural, not inherited.** A module satisfies the protocol by having the right functions. There
is no base class to subclass and therefore no shared implementation to accidentally depend on.

**Resolved lazily by name.** ``load_adapter("pi")`` imports ``f_modules.agent_pi`` only when it is
actually needed, so an absent binary — or an adapter module that imports something not installed —
never breaks an import while a different backend is in use.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from f_modules.data_types import AgentRequest, AgentResult, ToolCallRecord


class ToolCallTracker(Protocol):
    """Folds a backend's shapeless tool events into one record per real call (§8.9)."""

    def observe(self, event: dict[str, Any]) -> ToolCallRecord | None:
        """Consume one backend event; return a completed record, or ``None`` if still in flight."""
        ...


@runtime_checkable
class CodingAgentAdapter(Protocol):
    """What a backend module must provide. Duck-typed — no inheritance anywhere."""

    def run(
        self,
        request: AgentRequest,
        on_event: Callable[[dict[str, Any]], None],
        on_spawn: Callable[[int], None],
        on_exit: Callable[[int], None],
    ) -> AgentResult: ...

    def resolve_model(self, pattern: str) -> tuple[str, str]: ...

    def context_window(self, provider: str, model_id: str) -> int: ...

    def make_tool_tracker(self) -> ToolCallTracker: ...


class AdapterNotFound(Exception):
    """No adapter module for this ``coding_agent`` value.

    Raised during roster validation (§3.2), which runs before anything spawns — so a misconfigured
    backend costs nothing rather than being discovered four agent invocations in.
    """


_REQUIRED = ("run", "resolve_model", "context_window", "make_tool_tracker")


def adapter_module_name(coding_agent: str) -> str:
    return f"f_modules.agent_{coding_agent}"


def load_adapter(coding_agent: str) -> Any:
    """Import a backend adapter by name, checking it satisfies the protocol."""
    try:
        module = importlib.import_module(adapter_module_name(coding_agent))
    except ModuleNotFoundError as exc:
        raise AdapterNotFound(
            f"no adapter for coding_agent {coding_agent!r} "
            f"(expected {adapter_module_name(coding_agent)})"
        ) from exc

    missing = [name for name in _REQUIRED if not callable(getattr(module, name, None))]
    if missing:
        raise AdapterNotFound(
            f"adapter {coding_agent!r} is missing required callables: {', '.join(missing)}"
        )
    return module


def has_adapter(coding_agent: str) -> bool:
    """Whether an adapter is loadable — §3.2's second validation check."""
    try:
        load_adapter(coding_agent)
    except AdapterNotFound:
        return False
    return True
