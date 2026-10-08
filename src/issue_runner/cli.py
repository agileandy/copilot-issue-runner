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

from .config import ROLES, ConfigError, RoleConfig, load_config, validate_config
from .control import stop_signals
from .copilot import CopilotClient, CopilotError
from .demo.seed import DemoCleanIncomplete, clean_demo_clone, is_seed, load_demo_issue
from .github_flow import GitHubFlow
from .github_io import GithubError, comment_issue, fetch_issue, issue_from_file
from .orchestrator import run_issue
from .phases import preflight
from .phases.build import BuildError
from .phases.delivery import GATE_EXIT
from .phases.devops import DevopsError
from .phases.plan import PlanError, build_plan_prompt
from .phases.verify import VerifyError
from .ticket_mirror import GiteaTickets, GithubTickets
from .tickets import DOD_GATES, StateError
from .toolchain import ProvisionError
from .trackers import TrackerError, fetch_gitea_issue, resolve

# failures that abort a whole run: report them, never traceback at the user
PipelineError = (
    PlanError,
    CopilotError,
    DevopsError,
    BuildError,
    VerifyError,
    StateError,
    GithubError,
    TrackerError,
    ProvisionError,
)


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        # follow the name the user actually typed: issue-runner or gh-runner
        prog=prog or Path(sys.argv[0]).name or "issue-runner",
        description="Drive GitHub Copilot CLI through plan/build/verify to implement an issue.",
    )
    p.add_argument("issue", nargs="?", help="GitHub issue number (in --repo or the --dir repo)")
    p.add_argument("--issue-file", type=Path, help="read the issue from a markdown file instead")
    p.add_argument("--repo", help="owner/name for gh (default: inferred by gh from --dir)")
    p.add_argument("--dir", type=Path, default=Path.cwd(), help="target repository directory")
    p.add_argument("--config", type=Path, help="runner.toml path (default: <dir>/runner.toml)")
    p.add_argument("--test-cmd", help="test command template, e.g. 'pytest {test_path} -q'")
    p.add_argument("--regression-cmd", help="full regression suite command, with no file selector")
    p.add_argument(
        "--setup-cmd",
        action="append",
        help="command that prepares the run worktree's dependencies (repeatable)",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="use the supplied clean checkout instead of a separate managed run worktree",
    )
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
        "--deploy",
        action="store_true",
        help="take the issue through code review, merge and the Dev deployment; "
        "exit 0 only when its Definition of Done is met",
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
    p.add_argument(
        "--demo",
        action="store_true",
        help="clone permanent seed #54 and run the clone with real Copilot calls and credits",
    )
    p.add_argument(
        "--demo-clean",
        type=int,
        metavar="NUMBER",
        help="delete a demo clone and its sub-issues (never the seed)",
    )
    p.add_argument("--copilot-cmd", help="copilot binary to invoke (default: copilot)")
    p.add_argument("--visual", action="store_true", help="use the interactive visual terminal mode")
    p.add_argument(
        "--agent",
        action="store_true",
        help="post the run summary as a comment on the issue instead of printing it",
    )
    p.add_argument(
        "--comment-issue",
        type=int,
        metavar="NUMBER",
        help="with --agent, post the run summary to this issue (required with --issue-file)",
    )
    p.add_argument("--model", help="model for every role this run, overriding runner.toml (see 'copilot /model')")
    p.add_argument("--effort", help="reasoning effort for every role this run, overriding runner.toml")
    p.add_argument(
        "--role-model",
        action="append",
        metavar="ROLE=MODEL",
        help="override one role's model for this run, taking precedence over --model (repeatable)",
    )
    p.add_argument("--max-ai-credits", type=int, help="per-call AI credit soft cap (min 30)")
    p.add_argument(
        "--max-run-credits",
        type=int,
        help="soft whole-run credit budget, checked between Copilot invocations (exit 4)",
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
    if args.demo:
        return load_demo_issue(cfg, dry_run=args.dry_run)
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

    if args.demo_clean is not None:
        if args.demo or args.issue or args.issue_file:
            print("error: use --demo-clean on its own", file=sys.stderr)
            return 2
        try:
            clean_demo_clone(args.dir.resolve(), args.demo_clean)
        except DemoCleanIncomplete as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except (GithubError, DevopsError, TrackerError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if not args.issue and not args.issue_file and not args.demo:
        print("error: provide an issue number or --issue-file", file=sys.stderr)
        return 2

    if args.demo and (args.issue or args.issue_file):
        print("error: use --demo without an issue number or --issue-file", file=sys.stderr)
        return 2

    if args.deploy:
        conflicts = [
            flag
            for flag, value in (
                ("--no-pr", args.no_pr),
                ("--plan-only", args.plan_only),
                ("--issue-file", args.issue_file),
                ("--demo", args.demo),
                ("--in-place", args.in_place),
            )
            if value
        ]
        if conflicts:
            print(f"error: --deploy cannot be used with {', '.join(conflicts)}", file=sys.stderr)
            return 2

    if args.comment_issue is not None and not args.agent:
        print("error: --comment-issue only applies with --agent", file=sys.stderr)
        return 2

    if args.agent and args.issue_file and args.comment_issue is None:
        print(
            "error: --agent with --issue-file needs --comment-issue NUMBER to post to",
            file=sys.stderr,
        )
        return 2

    repo_dir = args.dir.resolve()
    try:
        cfg = load_config(repo_dir, args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.repo:
        cfg.repo = args.repo
    if args.test_cmd:
        cfg.test_cmd = args.test_cmd
        cfg.test_cmd_detected = False
    if args.setup_cmd:
        cfg.setup_cmd = args.setup_cmd
    if args.regression_cmd:
        cfg.regression_cmd = args.regression_cmd
    if args.in_place:
        cfg.isolate_worktree = False
    if args.max_rounds is not None:
        cfg.max_rounds = args.max_rounds
    if args.no_github_tickets:
        cfg.github_tickets = False
    if args.no_pr:
        cfg.open_pr = False
    if args.deploy:
        cfg.deploy = True
        cfg.open_pr = True
    if args.retry_blocked:
        cfg.retry_blocked = True
    if args.visual:
        cfg.visual = True
    if args.agent and not args.visual:
        # a visual mode set in runner.toml would draw to the terminal; an
        # explicit --visual is the caller asking for the display
        cfg.visual = False
    if args.copilot_cmd:
        cfg.copilot_cmd = args.copilot_cmd
    if not args.demo and args.copilot_cmd is None:
        cfg.copilot_cmd = _resolve_copilot_cmd(cfg.copilot_cmd)
    if args.max_ai_credits is not None:
        cfg.max_ai_credits = args.max_ai_credits
    if args.max_run_credits is not None:
        cfg.max_run_credits = args.max_run_credits
    if args.model or args.effort:
        for role in ROLES:
            existing = cfg.roles.get(role, RoleConfig())
            cfg.roles[role] = RoleConfig(
                model=args.model or existing.model,
                effort=args.effort or existing.effort,
            )
    for value in args.role_model or []:
        role, _, model_id = value.partition("=")
        if not role or not model_id or role not in ROLES:
            print(
                f"error: --role-model expects ROLE=MODEL with ROLE one of {', '.join(ROLES)}",
                file=sys.stderr,
            )
            return 2
        cfg.roles[role] = RoleConfig(model=model_id, effort=cfg.role(role).effort)

    try:
        validate_config(cfg)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        issue = _load_issue(args, cfg, repo_dir)
        if not args.demo and is_seed(issue, cfg.repo):
            raise GithubError("issue #54 is a permanent seed; use --demo to run a fresh clone")
    except (TrackerError, GithubError, DevopsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    post = None
    if args.agent and not args.dry_run:
        post = _summary_poster(cfg, repo_dir, issue, args.comment_issue)
        if post is None:
            print(
                "error: --agent cannot comment on this issue: no GitHub repo or Gitea API base "
                "(set GITEA_URL)",
                file=sys.stderr,
            )
            return 2

    if args.deploy:
        if not cfg.repo:
            print(
                "error: --deploy needs a GitHub repository (--repo or a github.com origin)",
                file=sys.stderr,
            )
            return 2
        try:
            cfg.preflight = preflight.run(cfg, issue, GitHubFlow(cfg.repo))
        except preflight.PreflightError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for line in cfg.preflight.lines():
            if args.dry_run:
                print(line)
            else:
                logging.getLogger("issue_runner").info(line)

    client = CopilotClient(cfg)
    if args.dry_run:
        preview = dict(issue, number="<new clone>") if args.demo else issue
        prompt = build_plan_prompt(
            preview, cfg, cfg.preflight.criteria if cfg.preflight is not None else None
        )
        argv_preview = client._build_argv(
            prompt, role="planner", read_only=True, session_name="planner"
        )
        print("dry run — planner invocation would be:")
        print(" ".join(shlex.quote(a) for a in argv_preview[:-1] if len(a) < 200))
        print(f"\n--- prompt ({len(prompt)} chars) ---\n{prompt}")
        return 0

    resume = (
        _demo_resume_command(cfg, issue, list(sys.argv[1:] if argv is None else argv))
        if args.demo
        else None
    )
    with stop_signals(cfg):
        return _execute(cfg, client, issue, args.plan_only, resume, post)


def _summary_poster(cfg, repo_dir: Path, issue: dict, comment_on: int | None = None):
    """A callable that posts a body as a comment on the run's issue, or None."""
    number = comment_on or issue.get("number")
    if not number:
        return None
    if cfg.repo:
        return lambda body: comment_issue(cfg.repo, number, body)
    import os

    try:
        info = resolve(repo_dir)
    except TrackerError:
        return None
    if info.kind == "github":
        # an --issue-file run never sets cfg.repo, but its origin still names it
        return lambda body: comment_issue(info.owner_repo, number, body)
    api_base = info.api_base or os.environ.get("GITEA_URL")
    if not api_base:
        return None
    tracker = GiteaTickets(api_base, info.owner_repo)
    return lambda body: tracker.comment_issue(number, body)


def _output(lines: list[str], post=None) -> None:
    """Print the summary, or in agent mode post it to the issue instead."""
    if post is not None:
        body = "**issue-runner run summary**\n\n```text\n" + "\n".join(lines).strip() + "\n```"
        try:
            post(body)
            return
        except (GithubError, TrackerError, OSError) as e:
            print(f"warning: could not post the run summary to the issue: {e}", file=sys.stderr)
    for line in lines:
        print(line)


def _execute(cfg, client, issue, plan_only, resume: str | None, post=None) -> int:
    if cfg.visual and sys.stdout.isatty() and sys.stdin.isatty():
        from .visual_display import VisualUnavailable, run_visual

        try:
            report, error, _detached = run_visual(cfg, client, issue, plan_only=plan_only)
        except VisualUnavailable as e:
            cfg.events = None
            logging.getLogger("issue_runner").warning("%s — falling back to the text visual", e)
        else:
            if error is not None:
                print(f"error: {error}", file=sys.stderr)
                lines = _abort_lines(error, client, resume)
                if post is not None:
                    # stderr already has the error; the comment must carry it too
                    lines = [f"error: {error}", *lines]
                _output(lines, post)
                return 1
            _output(_summary_lines(report, resume), post)
            return _exit_code(report)

    try:
        report = run_issue(cfg, client, issue, plan_only=plan_only)
    except PipelineError as e:
        return _report_abort(e, client, resume, post=post)
    except Exception as e:  # a demo must not end in a traceback
        logging.getLogger("issue_runner").debug("unexpected failure", exc_info=True)
        return _report_abort(e, client, resume, unexpected=True, post=post)

    _output(_summary_lines(report, resume), post)
    return _exit_code(report)


def _report_abort(error, client, resume, unexpected: bool = False, post=None) -> int:
    label = f"{type(error).__name__}: {error}" if unexpected else str(error)
    print(f"error: {label}", file=sys.stderr)
    lines = _abort_lines(error, client, resume)
    if post is not None:
        # stderr already has the error; the comment must carry it too
        lines = [f"error: {label}", *lines]
    _output(lines, post)
    return 1


def _abort_lines(error: BaseException, client, resume: str | None) -> list[str]:
    return [*_abort_usage_lines(client), *_partial_lines(error), *_demo_resume_lines(resume)]


def _partial_lines(error: BaseException) -> list[str]:
    """What an aborted run achieved, so the work can still be found."""
    report = getattr(error, "report", None)
    if report is None:
        return []
    lines = ["", f"branch: {report.branch or '(none)'}"]
    if report.worktree:
        lines.append(f"worktree: {report.worktree}")
    lines.append(f"tickets done: {report.done}, blocked: {report.blocked}")
    lines += [f"  - {detail}" for detail in report.details]
    return lines


def _demo_resume_command(cfg, issue, original_args: list[str]) -> str:
    args = ["gh-runner", str(issue["number"])]
    args.extend(arg for arg in original_args if arg not in ("--demo", "--plan-only"))
    args += ["--dir", str(cfg.repo_dir), "--no-pr"]
    if "--copilot-cmd" not in args:
        args += ["--copilot-cmd", cfg.copilot_cmd]
    return shlex.join(args)


def _exit_code(report) -> int:
    """4 (budget stop) is distinct from 3 (blocked) so queue callers can retry."""
    if report.stopped:
        return 130
    if report.budget_exhausted:
        return 4
    if report.blocked:
        return 3
    if getattr(report, "deploy", False) and not report.dod_met:
        # 5 criteria, 6 review, 7 merge, 8 deploy: anything short of done is a fail
        return GATE_EXIT.get(report.dod_failed_gate, 1)
    return 0


def _summary_lines(report, resume: str | None = None) -> list[str]:
    lines = ["", f"branch: {report.branch or '(plan only)'}"]
    if report.worktree:
        lines.append(f"worktree: {report.worktree}")
    lines.append(f"tickets done: {report.done}, blocked: {report.blocked}")
    if getattr(report, "deploy", False):
        lines += _dod_lines(report)
    if report.stopped:
        lines.append("Run stopped. State and worktree preserved.")
        if resume is None:
            lines.append("Re-run the same command to resume.")
    if report.budget_exhausted:
        lines.append("run stopped: AI credit budget exhausted — re-run to resume")
    if report.usage_summary:
        lines.append(report.usage_summary)
    if report.budget_summary:
        lines.append(report.budget_summary)
    if report.pr_url:
        lines.append(f"pull request: {report.pr_url}")
    lines += [f"  - {line}" for line in report.details]
    return lines + _demo_resume_lines(resume)


def _dod_lines(report) -> list[str]:
    verdict = "met" if report.dod_met else f"FAILED at {report.dod_failed_gate or 'tickets'}"
    shown = {"pass": "pass", "fail": "FAILED", None: "not reached"}
    return [f"definition of done: {verdict}"] + [
        f"  {gate:<15} {shown[report.gates.get(gate)]}" for gate in DOD_GATES
    ]


def _demo_resume_lines(resume: str | None) -> list[str]:
    if resume is None:
        return []
    return [
        "",
        f"Resume this clone: {resume}",
        "Using --demo again creates a fresh clone instead.",
    ]


def _abort_usage_lines(client: CopilotClient) -> list[str]:
    if not client.usage.calls:
        return []
    lines = [client.usage.summary_line()]
    if client.budget.limit is not None or not client.budget.cost_is_complete:
        lines.append(client.budget.describe())
    return lines


def gh_main(argv=None) -> int:
    """Entry point for the `gh-runner` command; delegates to main."""
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
