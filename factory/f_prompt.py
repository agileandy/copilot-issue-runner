#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_prompt — the smallest factory workflow: one engineer request, one agent.

Phases:
    1. request  (engineer)  capture the ask
    2. <agent>  (agent)     one turn, typed envelope out, gates verified

The unit of repeatability, and the smoke test for everything underneath it. If this runs, the
schemas, the trace, the phase primitive, the adapter, the parse-and-gate pipeline and the write
boundary are all working together.

This script is deliberately thin (§10.3): required agents, config load and validate, session
ensure, the phase sequence, envelope wiring, and ``run.finish()``. Anything else belongs in
``f_modules/``.

The inline dependency block above mirrors ``pyproject.toml``, so this file also runs in a repo the
factory was stamped into that has no Python project of its own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, session  # noqa: E402
from f_modules.data_types import AgentCall, PhaseKind, PhaseParams  # noqa: E402

REQUIRED_AGENTS = ["planner"]
"""The default. ``--agent`` overrides it, and whatever is chosen is validated before anything
spawns — a misnamed agent must cost nothing (§3.2)."""


def main(
    prompt: str,
    agent: str = "planner",
    config: str = agents.DEFAULT_CONFIG,
    f_id: str | None = None,
) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, [agent])  # nothing spawns before this passes

    run = session.ensure(cfg, f_id, fw_name="f_prompt", request=prompt)
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
            name=agent,
            kind=PhaseKind.AGENT,
            owner=agent,
            retries=1,
            description=roster[agent].purpose
            or f"Hand the request to {agent} and take back one typed report",
        )
    ) as ph:
        envelope = ph.call(
            AgentCall(
                output_type=agents.output_type_for(agent),
                gates=[gates.artifacts_exist, gates.files_non_empty],
            )
        )

    return run.finish(
        accepted=not envelope.failed,
        reason="the agent did not report success",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--agent", default="planner", help="which agent from the roster")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument(
        "--f-id",
        dest="f_id",
        default=None,
        help="join an existing session instead of minting a new one",
    )
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.agent, args.config, args.f_id))
