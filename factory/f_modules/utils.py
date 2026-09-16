"""Ids, timestamps, and the operator's environment.

Small shared plumbing that the trace needs before anything larger exists. Session id *minting and
joining* is a bigger question and belongs to ``session.py`` (§10.5) — this module only knows how to
make an id, not which one a run should use.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime


def new_id(prefix: str = "") -> str:
    """A fresh identifier, optionally prefixed so ids are readable in a trace."""
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw


def new_f_id() -> str:
    """A fresh run id — short enough to type after ``--f-id``, long enough not to collide."""
    return uuid.uuid4().hex[:8]


def utc_now() -> datetime:
    """Now, in UTC and timezone-aware.

    Timestamps are stored as ISO-8601 text: sortable as strings, readable without a decoder, and
    free of the deprecated implicit datetime adapters in Python's sqlite3.
    """
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def operator_env() -> dict[str, str]:
    """The environment a child process should see (§7.4).

    Child processes must see **the operator's own** PATH, toolchains and globally installed
    packages. A harness launched under an ephemeral venv runner (``uv run``) inherits a prepended
    venv bin directory holding the *harness's* dependencies, not the operator's — so it is stripped
    before the environment is handed to any child. Left in place, ``python3`` inside an agent's bash
    silently becomes the wrong interpreter.
    """
    env = dict(os.environ)
    venv = env.get("VIRTUAL_ENV")
    if not venv:
        return env

    venv_bin = os.path.join(venv, "bin")
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != venv_bin]
    env["PATH"] = os.pathsep.join(path_parts)
    env.pop("VIRTUAL_ENV", None)
    return env
