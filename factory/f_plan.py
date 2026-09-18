#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_plan — turn a request into a plan, before any code exists to blur it.

Phases:
    1. request  (engineer)
    2. plan     (agent/planner)

Use when you want the spec settled before committing to work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, changes, gates, git_helper, quality, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    BuildOutput,
    ChangeCapture,
    PhaseKind,
    PhaseParams,
    PlanOutput,
)

REQUIRED_AGENTS = ['planner']
MAX_FIX_LOOPS = 3

def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_plan", request=prompt)
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
            name="plan",
            kind=PhaseKind.AGENT,
            owner="planner",
            retries=1,
            description="Settle what to build while changing it is still free",
        )
    ) as ph:
        plan = ph.call(
            AgentCall(
                output_type=PlanOutput,
                gates=[gates.artifacts_exist, gates.files_non_empty],
            )
        )

    return run.finish(accepted=not plan.failed, reason="the planner did not report success")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
