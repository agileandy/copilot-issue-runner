"""Shared user stop request for the CLI, display and pipeline."""

import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .events import emit

if TYPE_CHECKING:
    from .config import RunnerConfig


class RunStopped(RuntimeError):
    pass


@dataclass
class RunControl:
    # Requests come from the main thread, including its signal handlers.
    requested: bool = False
    interrupt: bool = False

    def request(self) -> str:
        self.interrupt = self.requested
        self.requested = True
        if self.interrupt:
            return "Stopping and cleaning up... interrupting the current model call."
        return (
            "Stopping and cleaning up... finishing the current operation. "
            "Press Ctrl+C again to interrupt the model call."
        )

    def check(self, *, interrupt_only: bool = False) -> None:
        if self.interrupt or (self.requested and not interrupt_only):
            raise RunStopped("stopped by user")


def request_stop(cfg: "RunnerConfig") -> None:
    message = cfg.control.request()
    if cfg.events is None:
        print(message, file=sys.stderr, flush=True)
    else:
        emit(cfg.events, "stop_requested", message=message)


@contextmanager
def stop_signals(cfg: "RunnerConfig"):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def stop(signum, frame):
        request_stop(cfg)

    try:
        for sig in previous:
            signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
