#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_plan_build — plan, build, commit. Small, well-understood work.

Phases:
    1. request      (engineer)
    2. plan         (agent/planner)
    3. commit_plan  (code/git)
    4. build        (agent/builder)
    5. commit_code  (code/git)

Two commits, two work products, two authors. Each commit carries **its own** author's message
(§5.4): the planner's describes the spec document, the builder's describes the code. A chain that
reused one for the other would leave a history where nothing means what it says.

Note that both commits are `code` phases. A commit is never buried inside an agent phase (§1.2) —
hiding determinism there makes the trace lie about who did what.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f_modules import agents, gates, git_helper, session  # noqa: E402
from f_modules.data_types import (  # noqa: E402
    AgentCall,
    BuildOutput,
    PhaseKind,
    PhaseParams,
    PlanOutput,
)

REQUIRED_AGENTS = ["planner", "builder"]


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)  # nothing spawns before this passes
    # This chain ends in a commit, so the repository is a precondition too — and one
    # discovered at the commit phase has already been paid for in full.
    git_helper.require_repo()

    run = session.ensure(cfg, f_id, fw_name="f_plan_build", request=prompt)
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

    with run.phase(
        PhaseParams(
            name="commit_code",
            kind=PhaseKind.CODE,
            owner="git",
            description="Record the implementation in the builder's own words",
        )
    ) as ph:
        sha = git_helper.commit_all(
            git_helper.commit_message_for(build, "feat: implement the plan"), cwd=Path.cwd()
        )
        ph.log(f"committed {sha[:8]}")

    return run.finish(
        accepted=not build.failed,
        reason="the builder did not report success",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
