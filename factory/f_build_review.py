#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_build_review — build, then confirm it is what was asked for.

Phases:
    1. request  (engineer)
    2. build    (agent/builder)
    3. review_N (agent/reviewer)  ] bounded loop, up to MAX_FIX_LOOPS
    4. revise_N (agent/builder)   ]

Use when correctness-against-the-request matters more than a suite does — or when there is no
suite to run. The reviewer asks a question no test can: *is this what was asked for?*

The reviewer is repository-read-only, so it can report a problem but not quietly correct it. That
is the point: a review that fixes what it finds leaves nobody knowing it was ever wrong.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    BuildOutput,
    PhaseKind,
    PhaseParams,
    ReviewOutput,
)

REQUIRED_AGENTS = ["builder", "reviewer"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)

    run = session.ensure(cfg, f_id, fw_name="f_build_review", request=prompt)
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
        build = ph.call(AgentCall(output_type=BuildOutput, gates=[gates.diff_matches_claims]))

    review = None
    for attempt in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(
            PhaseParams(
                name=f"review_{attempt}",
                kind=PhaseKind.AGENT,
                owner="reviewer",
                retries=1,
                description="Ask whether this is what was asked for, which no suite can answer",
            )
        ) as ph:
            review = ph.call(
                AgentCall(output_type=ReviewOutput, previous=build,
                          gates=[gates.verdict_consistent])
            )
            ph.log(f"approved={review.approved}, {len(review.blocking)} blocking")

        if review.approved:
            break

        with run.phase(
            PhaseParams(
                name=f"revise_{attempt}",
                kind=PhaseKind.AGENT,
                owner="builder",
                retries=1,
                description="Address exactly what the reviewer blocked, and nothing more",
            )
        ) as ph:
            build = ph.call(
                AgentCall(output_type=BuildOutput, previous=review,
                          gates=[gates.diff_matches_claims])
            )

    return run.finish(
        accepted=bool(review and review.approved),
        reason=f"the review never came back approved within {MAX_FIX_LOOPS} attempts",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
