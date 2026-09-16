#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_build — implement a plan that already exists.

Phases:
    1. request  (engineer)
    2. build    (agent/builder)

Use when the plan is already written and only the code is missing.
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

REQUIRED_AGENTS = ['builder']
MAX_FIX_LOOPS = 3

def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_build", request=prompt)
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
            name="build",
            kind=PhaseKind.AGENT,
            owner="builder",
            retries=1,
            description="Turn the agreed plan into code that exists on disk",
        )
    ) as ph:
        build = ph.call(
            AgentCall(output_type=BuildOutput, gates=[gates.diff_matches_claims])
        )

    return run.finish(accepted=not build.failed, reason="the builder did not report success")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
