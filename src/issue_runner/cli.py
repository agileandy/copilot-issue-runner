"""Command-line entry point.

Examples:
    issue-runner 17 --repo owner/name --dir ~/src/thing
    issue-runner --issue-file ./issue.md --dir . --plan-only
    issue-runner 17 --model gpt-5-mini --max-ai-credits 30   # frugal mode
"""

import argparse
import logging
import shlex
import shutil
import sys
from pathlib import Path

from .config import ROLES, RoleConfig, load_config
from .copilot import CopilotClient
from .github_io import GithubError, fetch_issue, issue_from_file
from .orchestrator import run_issue
from .phases.plan import PLAN_PROMPT
from .ticket_mirror import GiteaTickets, GithubTickets
from .trackers import TrackerError, fetch_gitea_issue, resolve


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
        "--no-pr",
        action="store_true",
        help="do not open a pull request when the run finishes clean",
    )
    p.add_argument(
        "--retry-blocked",
        action="store_true",
        help="reset blocked tickets to pending and run them again",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planner invocation and exit without any model call",
    )
    p.add_argument("--copilot-cmd", help="copilot binary to invoke (default: copilot)")
    p.add_argument("--visual", action="store_true", help="use the interactive visual terminal mode")
    p.add_argument("--model", help="default model for all roles (see 'copilot /model')")
    p.add_argument("--effort", help="default reasoning effort for all roles")
    p.add_argument("--max-ai-credits", type=int, help="per-call AI credit soft cap (min 30)")
    p.add_argument(
        "--max-run-credits",
        type=int,
        help="whole-run AI credit budget; the run stops cleanly (exit 4) before exceeding it",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _resolve_copilot_cmd(cmd: str) -> str:
    if cmd != "copilot":
        return cmd
    fake = shutil.which("fake-copilot")
    if fake:
        return fake
    return cmd


def _load_issue(args, cfg, repo_dir: Path) -> dict:
    """Route the issue read: file > explicit GitHub --repo > origin remote (Gitea/GitHub)."""
    if args.issue_file:
        return issue_from_file(args.issue_file)
    if cfg.repo:
        if cfg.github_tickets:
            cfg.tickets_backend = GithubTickets(cfg.repo)
        return fetch_issue(args.issue, repo=cfg.repo)
    info = resolve(repo_dir)
    if info.kind == "gitea":
        import os

        api_base = info.api_base or os.environ.get("GITEA_URL")
        if cfg.github_tickets and api_base:
            cfg.tickets_backend = GiteaTickets(api_base, info.owner_repo)
        elif cfg.github_tickets:
            logging.getLogger("issue_runner").warning(
                "cannot mirror tickets: Gitea API base underivable from ssh remote "
                "(set GITEA_URL); tickets stay local in .issue-runner/"
            )
        return fetch_gitea_issue(api_base, info.owner_repo, args.issue)
    cfg.repo = info.owner_repo
    if cfg.github_tickets:
        cfg.tickets_backend = GithubTickets(cfg.repo)
    return fetch_issue(args.issue, repo=cfg.repo)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        force=True,
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
    if args.no_pr:
        cfg.open_pr = False
    if args.retry_blocked:
        cfg.retry_blocked = True
    if args.visual:
        cfg.visual = True
    if args.copilot_cmd:
        cfg.copilot_cmd = args.copilot_cmd
    cfg.copilot_cmd = _resolve_copilot_cmd(cfg.copilot_cmd)
    if args.max_ai_credits:
        cfg.max_ai_credits = args.max_ai_credits
    if args.max_run_credits:
        cfg.max_run_credits = args.max_run_credits
    if args.model or args.effort:
        for role in ROLES:
            existing = cfg.roles.get(role, RoleConfig())
            cfg.roles[role] = RoleConfig(
                model=existing.model or args.model,
                effort=existing.effort or args.effort,
            )

    try:
        issue = _load_issue(args, cfg, repo_dir)
    except (TrackerError, GithubError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

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

    if cfg.visual and sys.stdout.isatty():
        try:
            from .tui import run_visual
        except ImportError:
            logging.getLogger("issue_runner").warning(
                "textual is not installed — falling back to the text visual"
            )
        else:
            report, error, _detached = run_visual(cfg, client, issue, plan_only=args.plan_only)
            if error is not None:
                print(f"error: {error}", file=sys.stderr)
                return 1
            _print_summary(report)
            return _exit_code(report)

    report = run_issue(cfg, client, issue, plan_only=args.plan_only)

    _print_summary(report)
    return _exit_code(report)


def _exit_code(report) -> int:
    """4 (budget stop) is distinct from 3 (blocked) so queue callers can retry."""
    if report.budget_exhausted:
        return 4
    return 0 if report.blocked == 0 else 3


def _print_summary(report) -> None:
    print(f"\nbranch: {report.branch or '(plan only)'}")
    print(f"tickets done: {report.done}, blocked: {report.blocked}")
    if report.budget_exhausted:
        print("run stopped: AI credit budget exhausted — re-run to resume")
    if report.pr_url:
        print(f"pull request: {report.pr_url}")
    for line in report.details:
        print(f"  - {line}")


def gh_main(argv=None) -> int:
    """Entry point for the `gh-runner` command; delegates to main."""
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
