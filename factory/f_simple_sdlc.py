#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.9",
#     "pyyaml>=6.0",
# ]
# ///
"""f_simple_sdlc — the full chain, for work whose shape is not obvious.

Phases:
     1. request       (engineer)
     2. plan          (agent/planner)
     3. commit_plan   (code/git)
     4. build         (agent/builder)
     5. test_N        (code/quality)   ] bounded loop
     6. fix_N         (agent/builder)  ]
     7. review_N      (agent/reviewer) ] bounded loop
     8. revise_N      (agent/builder)  ]
     9. retest        (code/quality)   only if a revision touched the code
    10. commit_code   (code/git)
    11. changes       (code/git)
    12. document      (agent/documenter)
    13. commit_docs   (code/git)

Six things worth studying here (§13.1), of which the third is the one that is easy to get wrong:

1. **Three commits, three work products, three authors.** Plan, code and write-up each land in their
   own commit carrying their own producer's message.
2. **Two different questions, asked in order.** The suite asks *does it run*; the reviewer asks *is
   this what was asked for*. Neither can answer the other's, which is why both are here.
3. **Staleness is tracked.** A revision edits code *after* the last green test, so the suite's
   verdict no longer describes the tree on disk. Committing then would ship a tree nothing has
   verified — green from a test that ran against different code, approved by a reviewer who read it
   before the edit. So a revision sets a stale flag and forces a **retest**.
4. **Code lands last**, after verification rather than straight after the build. A failed run leaves
   the plan committed — a real record of what was asked — and the working tree dirty, where the
   engineer can see it.
5. **The baseline is pinned before the first commit**, because by documentation time this run has
   moved the branch itself, and a diff taken then would be measured against its own output.
6. **An empty diff is a structural guard**: the change capture is a code phase that raises before
   the documenter is ever spawned.
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
    DocumentOutput,
    PhaseKind,
    PhaseParams,
    PlanOutput,
    ReviewOutput,
)

REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = agents.DEFAULT_CONFIG, f_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    roster = agents.validate(cfg, REQUIRED_AGENTS)  # nothing spawns before this passes
    # A placeholder suite cannot be satisfied by building anything, so the fix loop
    # would spend three builder turns on it. Refused here, where it costs nothing.
    quality.require_configured()
    # This chain ends in a commit, so the repository is a precondition too — and one
    # discovered at the commit phase has already been paid for in full.
    git_helper.require_repo()

    run = session.ensure(cfg, f_id, fw_name="f_simple_sdlc", request=prompt)
    agents.attach(run, roster)
    session_dir = Path(cfg.defaults.data_dir) / "sessions" / run.f_id

    with run.phase(
        PhaseParams(
            name="request",
            kind=PhaseKind.ENGINEER,
            owner=run.engineer,
            description="Put the ask on record before anything acts on it",
        )
    ) as ph:
        ph.log(prompt)
        # Pinned now, before this run creates any commits of its own. By documentation time the
        # branch has moved, and a base resolved then would measure the run against its own output.
        baseline = git_helper.head(cwd=Path.cwd())
        ph.log(f"baseline pinned at {baseline[:8]}")

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
        ph.log(f"committed {git_helper.commit_all(git_helper.commit_message_for(plan, 'docs(specs): record the plan'), cwd=Path.cwd())[:8]}")

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

    # --- does it run? -------------------------------------------------------------------
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
            verdict = quality.as_envelope(quality.run_quality(cwd=Path.cwd()), "tests")
            ph.log(verdict.summary)

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
            build = ph.call(AgentCall(output_type=BuildOutput, previous=verdict,
                                      gates=[gates.diff_matches_claims]))

    if not (verdict and verdict.passed):
        return run.finish(
            accepted=False,
            reason=f"the suite never came back clean within {MAX_FIX_LOOPS} attempts",
        )

    # --- is it what was asked for? ------------------------------------------------------
    review = None
    stale = False
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
            review = ph.call(AgentCall(output_type=ReviewOutput, previous=build,
                                       gates=[gates.verdict_consistent]))
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
            build = ph.call(AgentCall(output_type=BuildOutput, previous=review,
                                      gates=[gates.diff_matches_claims]))
            # The code has moved since the last green suite, so that verdict is now about a tree
            # which no longer exists.
            stale = True

    if not (review and review.approved):
        return run.finish(
            accepted=False,
            reason=f"the review never came back approved within {MAX_FIX_LOOPS} attempts",
        )

    if stale:
        with run.phase(
            PhaseParams(
                name="retest",
                kind=PhaseKind.CODE,
                owner="quality",
                description="Re-run the suite because a revision moved the code out from under it",
            )
        ) as ph:
            verdict = quality.as_envelope(quality.run_quality(cwd=Path.cwd()), "retest")
            ph.log(verdict.summary)

        if not verdict.passed:
            return run.finish(
                accepted=False,
                reason="the revision that satisfied the reviewer broke the suite",
            )

    # The tree about to be committed is now the tree that was both tested and approved.
    with run.phase(
        PhaseParams(
            name="commit_code",
            kind=PhaseKind.CODE,
            owner="git",
            description="Record the verified implementation in the builder's own words",
        )
    ) as ph:
        ph.log(f"committed {git_helper.commit_all(git_helper.commit_message_for(build, 'feat: implement the plan'), cwd=Path.cwd())[:8]}")

    # --- write it up --------------------------------------------------------------------
    with run.phase(
        PhaseParams(
            name="changes",
            kind=PhaseKind.CODE,
            owner="git",
            description="Capture what this run actually changed, measured from the pinned baseline",
        )
    ) as ph:
        captured = changes.capture(
            ChangeCapture(ref=baseline, diff_path=str(session_dir / "diff.txt")), cwd=Path.cwd()
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

    with run.phase(
        PhaseParams(
            name="commit_docs",
            kind=PhaseKind.CODE,
            owner="git",
            description="Record the write-up in the documenter's own words",
        )
    ) as ph:
        ph.log(f"committed {git_helper.commit_all(git_helper.commit_message_for(written, 'docs: write up the change'), cwd=Path.cwd())[:8]}")

    return run.finish(accepted=True, reason="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="the engineer's ask")
    parser.add_argument("--config", default=agents.DEFAULT_CONFIG)
    parser.add_argument("--f-id", dest="f_id", default=None,
                        help="join an existing session instead of minting a new one")
    args = parser.parse_args()
    raise SystemExit(main(args.prompt, args.config, args.f_id))
