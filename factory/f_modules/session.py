"""Id minting and joining, signal handling, and ``Run`` construction.

``--f-id`` appears on every FW (§5.8). Given one, the run **joins** that session: the same
directories, the same ``context_handoff/``, and each agent resuming its existing context window via
``agent_map.json``. Omitted, a fresh id is minted and printed so the next FW can pick it up.

Two details that look like housekeeping and are not:

- A joined run **continues the phase sequence** from the highest existing ``seq``. Restarting at 1
  would collide on both ordering and phase id, silently overwriting the first run's rows.
- Changing an agent's **model** invalidates its resumable session and mints a new id; changing
  **thinking** does not. A resumed context window belongs to the model that built it.
"""

from __future__ import annotations

import atexit
import getpass
import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

from f_modules.console import Console
from f_modules.data_types import FactoryConfig, SessionRecord
from f_modules.runner import Run, RunKilled
from f_modules.tracer import Tracer
from f_modules.utils import new_f_id


class AgentMap:
    """``agent_map.json``: which backend session each agent is resuming (§5.8)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            self.entries = json.loads(path.read_text() or "{}")

    def session_id_for(self, agent: str, model: str, coding_agent: str) -> str:
        """The backend session id this agent should use, minting a new one when invalidated.

        Model change invalidates; thinking change does not — thinking is not even stored here, which
        is the cheapest way to guarantee it can never invalidate anything.
        """
        entry = self.entries.get(agent)
        if entry and entry.get("model") == model and entry.get("coding_agent") == coding_agent:
            return entry["session_id"]

        session_id = new_f_id()
        self.entries[agent] = {
            "session_id": session_id,
            "model": model,
            "coding_agent": coding_agent,
        }
        self.save()
        return session_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, sort_keys=True) + "\n")


def session_paths(config: FactoryConfig, f_id: str) -> dict[str, Path]:
    """The layout of one run's session directory (§5.8)."""
    data_dir = Path(config.defaults.data_dir)
    session_dir = data_dir / "sessions" / f_id
    return {
        "data_dir": data_dir,
        "session_dir": session_dir,
        "context_handoff": session_dir / "context_handoff",
        "agent_map": session_dir / "agent_map.json",
        "events": session_dir / "events.jsonl",
    }


def _resume_point(db_path: Path, f_id: str) -> tuple[int, int, float]:
    """What a joining run must carry forward: the phase sequence, and the spend so far.

    Both are cumulative properties of the **session**, not of one process. A joined run that starts
    its tally at zero does not merely under-report — it *overwrites* the earlier run's total on
    finish, so the session ends up claiming the last FW's spend as the whole chain's. Spend
    accumulates across every send (§8.8), and a chain is made of sends.
    """
    if not db_path.exists():
        return 0, 0, 0.0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        seq = conn.execute(
            "SELECT MAX(seq) AS top FROM phases WHERE f_id = ?", (f_id,)
        ).fetchone()[0]
        spent = conn.execute(
            "SELECT total_tokens, total_cost FROM sessions WHERE f_id = ?", (f_id,)
        ).fetchone()
        return int(seq or 0), int((spent or [0, 0])[0] or 0), float((spent or [0, 0])[1] or 0.0)
    except sqlite3.OperationalError:
        return 0, 0, 0.0
    finally:
        conn.close()


def install_signal_handlers() -> None:
    """Convert SIGTERM/SIGINT into a normal exception (§9.9).

    Default signal handling exits **without unwinding**, which would leave a session reading
    ``running`` forever and its process rows open — a trace claiming work is in flight that is
    already dead. Raising instead lets every context manager run on the way out, so the run
    finalizes itself as failed.

    SIGINT already raises ``KeyboardInterrupt`` and so already unwinds; **SIGTERM is the real
    hazard**, and is the reason this exists. Both are handled so the two paths are identical.
    """

    def handler(signum: int, _frame: Any) -> None:
        raise RunKilled(f"received signal {signal.Signals(signum).name}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def ensure(
    config: FactoryConfig,
    f_id: str | None = None,
    fw_name: str = "",
    request: str = "",
    engineer: str = "",
    console: Console | None = None,
    handle_signals: bool = True,
) -> Run:
    """Mint or join a session and construct its :class:`Run`."""
    joining = f_id is not None
    f_id = f_id or new_f_id()

    paths = session_paths(config, f_id)
    paths["session_dir"].mkdir(parents=True, exist_ok=True)
    paths["context_handoff"].mkdir(parents=True, exist_ok=True)

    db_path = Path(config.observability.db)
    start_seq, spent_tokens, spent_cost = (
        _resume_point(db_path, f_id) if joining else (0, 0, 0.0)
    )

    tracer = Tracer(db_path, paths["session_dir"], f_id)
    console = console or Console(tracer)
    console.tracer = tracer

    engineer = engineer or _operator_name()
    tracer.session_start(
        SessionRecord(
            f_id=f_id,
            f_name=_chain_name(tracer, f_id, fw_name),
            request=request,
            engineer=engineer,
        )
    )

    run = Run(
        f_id=f_id,
        config=config,
        tracer=tracer,
        console=console,
        engineer=engineer,
        start_seq=start_seq,
        request=request,
    )
    # Continue the session's tally rather than restarting it, or this run's finish would overwrite
    # the earlier run's total with its own.
    run.total_tokens = spent_tokens
    run.total_cost = spent_cost

    if joining:
        console.say(f"joined session {f_id}, continuing from phase {start_seq}")
    else:
        # Printed so the next FW in the chain can pick it up with --f-id.
        console.say(f"session {f_id}")

    if handle_signals:
        install_signal_handlers()
    # Covers the window a phase context manager cannot: a signal or an unhandled error arriving
    # *between* phases, which would otherwise leave the session reading `running` forever.
    atexit.register(run.finalize_if_abandoned)

    return run


def _chain_name(tracer: Tracer, f_id: str, fw_name: str) -> str:
    """``sessions.f_name`` accumulates chained FW names in run order (§9.3)."""
    if not fw_name:
        return ""
    row = tracer.conn.execute("SELECT f_name FROM sessions WHERE f_id = ?", (f_id,)).fetchone()
    existing = (row["f_name"] if row else "") or ""
    if not existing:
        return fw_name
    if fw_name in existing.split(" + "):
        return existing
    return f"{existing} + {fw_name}"


def _operator_name() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except Exception:  # pragma: no cover - getuser can fail in odd environments
        return "engineer"
