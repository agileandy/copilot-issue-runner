#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_scout — read-only recon. Nothing changes.

Phases:
    1. request  (engineer)
    2. scout    (agent/scout)

The one case where §11.5 says a single-agent chain is *right*: a question, not work to be done.
The scout is repository-read-only, enforced after the fact against the real tree — so "nothing
changes" is a checked claim rather than a promise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    ChangeCapture,
    DocumentOutput,
    PhaseKind,
    PhaseParams,
    ScoutOutput,
)

REQUIRED_AGENTS = ['scout']

def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_scout", request=prompt)
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
            name="scout",
            kind=PhaseKind.AGENT,
            owner="scout",
            retries=1,
            description="Answer where things live before anyone decides what to do about them",
        )
    ) as ph:
        findings = ph.call(AgentCall(output_type=ScoutOutput))
        ph.log(f"{len(findings.findings)} findings")

    return run.finish(accepted=not findings.failed, reason="the scout did not report success")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
