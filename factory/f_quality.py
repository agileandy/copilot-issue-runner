#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_quality — run the deterministic checks. No agent at all.

Phases:
    1. request  (engineer)
    2. quality  (code/quality)

The shortest possible demonstration of §7.1: if you can write the invocation down, no agent runs
it. This chain contains no agent, which is worth having precisely because it proves a chain need
not contain one — and because "run the suite" should never cost a model call.

A failing block does **not** fail its phase. The runner did its job; the code is what failed. The
run is what refuses to be accepted (§1.4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, quality, session  # noqa: E402
from f_modules.data_types import PhaseKind, PhaseParams  # noqa: E402

REQUIRED_AGENTS = []
"""Empty, and correct. Validation still runs — it simply has nothing to find fault with."""


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_quality", request=prompt)

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
            name="quality",
            kind=PhaseKind.CODE,
            owner="quality",
            description="Run every deterministic block and collect all of the failures at once",
        )
    ) as ph:
        result = quality.run_quality(cwd=Path.cwd())
        for check in result.checks:
            ph.log(f"{check.name}: exit {check.returncode}")
        for failure in result.failures:
            ph.log(failure.output_tail.splitlines()[-1] if failure.output_tail else failure.name)

    return run.finish(accepted=result.passed, reason="a quality block did not come back clean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
