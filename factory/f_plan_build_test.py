#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_plan_build_test — the standard chain.

Phases:
    1. request      (engineer)
    2. plan         (agent/planner)
    3. commit_plan  (code/git)
    4. build        (agent/builder)
    5. test_N       (code/quality)   ] bounded loop, up to MAX_FIX_LOOPS
    6. fix_N        (agent/builder)  ]
    7. commit_code  (code/git)

Use this when the work is real and has a suite.

**Code lands last.** The implementation is committed after verification, not straight after the
build. A failed run therefore leaves the plan committed — it is a real record of what was asked —
and the working tree dirty, where the engineer can see it and decide (§13.1).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, git_helper, quality, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    BuildOutput,
    PhaseKind,
    PhaseParams,
    PlanOutput,
)

REQUIRED_AGENTS = ["planner", "builder"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)
    # A placeholder suite cannot be satisfied by building anything, so the fix loop
    # would spend three builder turns on it. Refused here, where it costs nothing.
    quality.require_configured()
    # This chain ends in a commit, so the repository is a precondition too — and one
    # discovered at the commit phase has already been paid for in full.
    git_helper.require_repo()

    run = session.ensure(cfg, f_id, fw_name="f_plan_build_test", request=prompt)
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
            AgentCall(output_type=PlanOutput, gates=[gates.artifacts_exist, gates.files_non_empty])
        )

    with run.phase(
        PhaseParams(
            name="commit_plan",
            kind=PhaseKind.CODE,
            owner="git",
            description="Put the spec on record before any code exists to blur it",
        )
    ) as ph:
        sha = git_helper.commit_all(
            git_helper.commit_message_for(plan, "docs(specs): record the plan"), cwd=Path.cwd()
        )
        ph.log(f"committed {sha[:8]}")

    with run.phase(
        PhaseParams(
            name="build",
            kind=PhaseKind.AGENT,
            owner="builder",
            retries=1,
            description="Turn the agreed plan into code that exists on disk",
        )
    ) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, previous=plan,
                                  gates=[gates.diff_matches_claims]))

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
            build = ph.call(
                AgentCall(output_type=BuildOutput, previous=verdict,
                          gates=[gates.diff_matches_claims])
            )

    accepted = bool(verdict and verdict.passed)

    if accepted:
        # Code lands last, after verification — so the tree that gets committed is the tree that
        # was actually tested.
        with run.phase(
            PhaseParams(
                name="commit_code",
                kind=PhaseKind.CODE,
                owner="git",
                description="Record the verified implementation in the builder's own words",
            )
        ) as ph:
            sha = git_helper.commit_all(
                git_helper.commit_message_for(build, "feat: implement the plan"), cwd=Path.cwd()
            )
            ph.log(f"committed {sha[:8]}")

    return run.finish(
        accepted=accepted,
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
