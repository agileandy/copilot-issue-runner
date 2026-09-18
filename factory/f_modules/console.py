"""One narrative, two destinations.

§9.8 is an invariant, not a style preference: every line the harness prints **also** lands as a
``log`` event, emitted through a single helper, so the terminal and the trace cannot drift. If they
can drift, they will, and then the two accounts of a run disagree with no way to tell which lied.

Consequences that follow from that and are easy to undo by accident:

- **Never a bare** ``print()``. :meth:`Console.say` is the only thing in the codebase that writes to
  stdout, and ``tests/test_console.py`` fails the build if another module grows a ``print(``.
- **Plain sequential lines only** — no spinners, no progress bars, no live-updating displays. A CI
  log then reads exactly like a terminal, and a terminal reads exactly like the trace.
"""

from __future__ import annotations

import sys
from typing import Any

from f_modules.data_types import EventType
from f_modules.tracer import Tracer


class Console:
    """The single print-and-trace helper."""

    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer = tracer
        """``None`` only before a session exists.

        Roster validation (§3.2) runs *before* anything spawns, and therefore before there is a run
        to trace against, so its failures have nowhere to land but the terminal. Every other caller
        passes a tracer; a ``None`` here after the session is open is a bug, not a convenience.
        """

    def say(self, message: str, phase_id: str = "", **fields: Any) -> None:
        """Print one line and record it as a ``log`` event. The only stdout writer there is."""
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
        if self.tracer is not None:
            self.tracer.event(
                EventType.LOG,
                phase_id=phase_id,
                name="log",
                payload={"message": message, **fields},
            )

    def banner(self, message: str) -> None:
        """A terminal banner, which is still just lines — and still traced.

        §1.4 has ``run.finish()`` settle the persisted status, the banner and the exit code
        together so they cannot disagree; this renders the banner half of that.
        """
        rule = "=" * min(len(message), 78)
        self.say(rule)
        self.say(message)
        self.say(rule)

    def error(self, message: str, phase_id: str = "", **fields: Any) -> None:
        """An error line.

        It goes to stdout with everything else rather than to stderr, because interleaving two
        streams is how a CI log stops matching the trace it is supposed to mirror. The ``error``
        *event* type is for a phase that died (§9.5); this is narration about one.
        """
        self.say(f"ERROR: {message}", phase_id=phase_id, level="error", **fields)
