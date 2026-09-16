#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_build_test — build, then satisfy the suite.

Phases:
    1. request  (engineer)
    2. build    (agent/builder)
    3. test_N   (code/quality)   ] bounded loop, up to MAX_FIX_LOOPS
    4. fix_N    (agent/builder)  ]

Use when the code has a suite to satisfy.

Three bounded mechanisms meet here and must never be conflated (§10.4):

===============  ==========================  ==========================================
JSON retries     a fixed constant             a malformed response object
Gate retries     ``PhaseParams.retries``      valid JSON making unacceptable claims
Fix loop         ``MAX_FIX_LOOPS``, below     actual code that does not work
===============  ==========================  ==========================================

Only the third belongs to this file. The other two are the harness's, and it repairs those without
this script knowing.

The suite is a `code` phase because it is a **known command** (§7.1) — there is no tester agent, and
an agent rediscovering the test runner every run would spend a context window learning what a
subprocess already knows. A failing suite does not fail its phase: the runner did its job, and the
code is what failed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, quality, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    BuildOutput,
    PhaseKind,
    PhaseParams,
)

REQUIRED_AGENTS = ["builder"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)
    # A placeholder suite cannot be satisfied by building anything, so the fix loop
    # would spend three builder turns on it. Refused here, where it costs nothing.
    quality.require_configured()

    run = session.ensure(cfg, f_id, fw_name="f_build_test", request=prompt)
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
            description="Turn the request into code that exists on disk",
        )
    ) as ph:
        ph.call(AgentCall(output_type=BuildOutput, gates=[gates.diff_matches_claims]))

    verdict = None
    for attempt in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(
            PhaseParams(
                name=f"test_{attempt}",
                kind=PhaseKind.CODE,
                owner="quality",
                description="Ask whether the code runs, which is a command rather than a judgement",
            )
        ) as ph:
            result = quality.run_quality(cwd=Path.cwd())
            verdict = quality.as_envelope(result, "tests")
            ph.log(verdict.summary)
            for failure in verdict.failures:
                ph.log(failure.splitlines()[0])

        if verdict.passed:
            break

        with run.phase(
            PhaseParams(
                name=f"fix_{attempt}",
                kind=PhaseKind.AGENT,
                owner="builder",
                retries=1,
                description="Repair exactly what the suite reported, and nothing more",
            )
        ) as ph:
            # The failure arrives as an envelope (§5.3), so the builder cannot tell a subprocess
            # from an agent — and swapping one for the other leaves this loop untouched.
            ph.call(
                AgentCall(
                    output_type=BuildOutput,
                    previous=verdict,
                    gates=[gates.diff_matches_claims],
                )
            )

    # A test phase that ran a red suite succeeded. The run must not (§1.4).
    return run.finish(
        accepted=bool(verdict and verdict.passed),
        reason=f"the suite never came back clean within {MAX_FIX_LOOPS} attempts",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
