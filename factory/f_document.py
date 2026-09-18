#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_document — write up work that has already been done.

Phases:
    1. request  (engineer)
    2. changes  (code/git)
    3. document (agent/documenter)

The change capture is a **code** phase that raises on an **empty diff** (§13.1), before the
documenter is ever spawned. Spawning an agent to describe nothing is not a graceful degradation; it
is a paid apology.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, changes, gates, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    ChangeCapture,
    DocumentOutput,
    PhaseKind,
    PhaseParams,
    ScoutOutput,
)

REQUIRED_AGENTS = ['documenter']

def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_document", request=prompt)
    agents.attach(run, roster)

    with run.phase(
        PhaseParams(
            name="request",
            kind=PhaseKind.ENGINEER,
            owner=run.engineer,
            description="Put the ask on record before anything acts on it",
        )
    ) as ph:
        ph.log(prompt)

    with run.phase(
        PhaseParams(
            name="changes",
            kind=PhaseKind.CODE,
            owner="git",
            description="Capture what actually changed, against a base that records why it was chosen",
        )
    ) as ph:
        captured = changes.capture(
            ChangeCapture(diff_path=str(Path(cfg.defaults.data_dir) / "sessions" / run.f_id / "diff.txt")),
            cwd=Path.cwd(),
        )
        if captured.is_empty:
            raise ValueError("the diff is empty; there is nothing to document")
        ph.log(f"{len(captured.changed_files)} files against {captured.base} ({captured.reason.value})")
        diff = changes.as_envelope(captured)

    with run.phase(
        PhaseParams(
            name="document",
            kind=PhaseKind.AGENT,
            owner="documenter",
            retries=1,
            description="Explain the change to someone who was not here while it happened",
        )
    ) as ph:
        written = ph.call(
            AgentCall(output_type=DocumentOutput, previous=diff,
                      gates=[gates.artifacts_exist, gates.files_non_empty])
        )

    return run.finish(accepted=not written.failed, reason="the documenter did not report success")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
