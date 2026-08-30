"""Command-line entry point.

Examples:
    issue-runner 17 --repo owner/name --dir ~/src/thing
    issue-runner --issue-file ./issue.md --dir . --plan-only
    issue-runner 17 --model gpt-5-mini --max-ai-credits 30   # frugal mode
"""

import argparse
import logging
import shlex
import sys
from pathlib import Path

from .config import ROLES, RoleConfig, load_config
from .copilot import CopilotClient
from .github_io import fetch_issue, issue_from_file
from .orchestrator import run_issue
from .phases.plan import PLAN_PROMPT


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue-runner",
        description="Drive GitHub Copilot CLI through plan/build/verify to implement an issue.",
    )
    p.add_argument("issue", nargs="?", help="GitHub issue number (in --repo or the --dir repo)")
    p.add_argument("--issue-file", type=Path, help="read the issue from a markdown file instead")
    p.add_argument("--repo", help="owner/name for gh (default: inferred by gh from --dir)")
    p.add_argument("--dir", type=Path, default=Path.cwd(), help="target repository directory")
    p.add_argument("--config", type=Path, help="runner.toml path (default: <dir>/runner.toml)")
    p.add_argument("--test-cmd", help="test command template, e.g. 'pytest {test_path} -q'")
    p.add_argument("--max-rounds", type=int, help="max verifier hand-backs per ticket")
    p.add_argument(
        "--no-github-tickets",
        action="store_true",
        help="do not mirror tickets as GitHub sub-issues",
    )
    p.add_argument("--plan-only", action="store_true", help="stop after the plan phase")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planner invocation and exit without any model call",
    )
    p.add_argument("--copilot-cmd", help="copilot binary to invoke (default: copilot)")
    p.add_argument("--model", help="default model for all roles (see 'copilot /model')")
    p.add_argument("--effort", help="default reasoning effort for all roles")
    p.add_argument("--max-ai-credits", type=int, help="per-call AI credit soft cap (min 30)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not args.issue and not args.issue_file:
        print("error: provide an issue number or --issue-file", file=sys.stderr)
        return 2

    repo_dir = args.dir.resolve()
    cfg = load_config(repo_dir, args.config)
    if args.repo:
        cfg.repo = args.repo
    if args.test_cmd:
        cfg.test_cmd = args.test_cmd
    if args.max_rounds is not None:
        cfg.max_rounds = args.max_rounds
    if args.no_github_tickets:
        cfg.github_tickets = False
    if args.copilot_cmd:
        cfg.copilot_cmd = args.copilot_cmd
    if args.max_ai_credits:
        cfg.max_ai_credits = args.max_ai_credits
    if args.model or args.effort:
        for role in ROLES:
            existing = cfg.roles.get(role, RoleConfig())
            cfg.roles[role] = RoleConfig(
                model=existing.model or args.model,
                effort=existing.effort or args.effort,
            )

    issue = (
        issue_from_file(args.issue_file)
        if args.issue_file
        else fetch_issue(args.issue, repo=cfg.repo)
    )

    client = CopilotClient(cfg)
    if args.dry_run:
        prompt = PLAN_PROMPT.format(
            number=issue["number"], title=issue["title"], body=issue["body"], feedback=""
        )
        argv_preview = client._build_argv(
            prompt, role="planner", read_only=True, session_name="planner"
        )
        print("dry run — planner invocation would be:")
        print(" ".join(shlex.quote(a) for a in argv_preview[:-1] if len(a) < 200))
        print(f"\n--- prompt ({len(prompt)} chars) ---\n{prompt}")
        return 0

    report = run_issue(cfg, client, issue, plan_only=args.plan_only)

    print(f"\nbranch: {report.branch or '(plan only)'}")
    print(f"tickets done: {report.done}, blocked: {report.blocked}")
    for line in report.details:
        print(f"  - {line}")
    return 0 if report.blocked == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
