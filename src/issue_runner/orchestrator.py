"""Orchestrator: isolated workspace, saved plan, and checkpointed ticket phases.

Routing per ticket (Andy's spec):
  builder.tester (red enforced) -> builder.coder (green enforced) -> verifier
    refine_test -> back to tester (red not required: code exists) -> coder if red
    rework_code -> back to coder
    pass        -> deterministic regression gate -> approved commit, ticket done
Unaccepted edits remain in the run workspace and never enter a later ticket.
"""

import base64
import hashlib
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import github_io
from .budget import BudgetExhausted, RunBudget
from .config import RunnerConfig
from .control import RunStopped, announce_stop
from .copilot import CopilotError
from .demo.seed import is_seed
from .events import emit, ticket_snapshot
from .github_io import GithubError
from .journal import Journal
from .phases import devops
from .phases.build import (
    BuildError,
    CoderFailure,
    TestAlreadyPasses,
    coder_step,
    resolve_test_path,
    run_test_command,
    run_tests,
    tester_step,
)
from .phases.devops import DevopsError
from .phases.plan import plan_step
from .phases.verify import VerifyError, verify_step
from .testcmd import detect_regression_cmd
from .tickets import StateError, Ticket, TicketStore
from .visual import render_flow

log = logging.getLogger("issue_runner")


@dataclass
class RunReport:
    branch: str = ""
    done: int = 0
    blocked: int = 0
    pr_url: str = ""
    budget_exhausted: bool = False
    usage_summary: str = ""
    budget_summary: str = ""
    worktree: str = ""
    plan_only: bool = False
    stopped: bool = False
    details: list[str] = field(default_factory=list)


class RegressionFailure(BuildError):
    pass


def _render_visual(
    cfg: RunnerConfig,
    *,
    plan: str | None = None,
    branch: str | None = None,
    tickets: list[Ticket] | None = None,
) -> None:
    if not cfg.visual:
        return
    payload = {
        "plan": plan if plan is not None else "pending",
        "branch": branch if branch is not None else "pending",
        "tickets": [{"id": ticket.id, "status": ticket.status} for ticket in (tickets or [])],
    }
    print(render_flow(payload), flush=True)


def run_issue(
    cfg: RunnerConfig,
    client,
    issue: dict,
    state_dir: Path | None = None,
    plan_only: bool = False,
) -> RunReport:
    if is_seed(issue, cfg.repo):
        raise DevopsError("refusing to execute permanent seed #54; use --demo to create a clone")
    source = Path(cfg.repo_dir).resolve()
    if not source.is_dir():
        raise DevopsError(f"target repository directory does not exist: {source}")
    original_dir = cfg.repo_dir
    client_config = getattr(client, "config", None)
    original_client_dir = client_config.repo_dir if client_config is not None else None
    if issue["number"]:
        issue_ref, branch_slug = str(issue["number"]), devops.slugify(issue["title"])
    else:
        issue_ref, branch_slug = devops.slugify(issue["title"], 20), ""
    state_dir = Path(state_dir) if state_dir is not None else source / ".issue-runner"
    if not state_dir.is_absolute():
        state_dir = source / state_dir
    state_dir = state_dir.resolve()
    store = TicketStore(state_dir, issue_ref=issue_ref)
    report = RunReport(plan_only=plan_only)
    lock = nullcontext() if plan_only else devops.repository_lock(source)
    try:
        with devops.state_lock(state_dir), lock:
            try:
                store.load()
                if store.source_repo and Path(store.source_repo).resolve() != source:
                    raise StateError("saved state belongs to a different source repository")
                store.source_repo = str(source)
                if not plan_only:
                    _prepare_workspace(cfg, store, source, branch_slug)
                    if client_config is not None:
                        client_config.repo_dir = cfg.repo_dir
                    report.branch = store.branch or ""
                    report.worktree = str(cfg.repo_dir)
                return _run_issue(cfg, client, issue, store, report, plan_only)
            except RunStopped:
                for ticket in store.tickets:
                    if ticket.status == "in_progress":
                        ticket.status = "pending"
                store.save()
                report.stopped = True
                report.done = sum(t.status == "done" for t in store.tickets)
                report.blocked = sum(t.status == "blocked" for t in store.tickets)
                report.details.append("run stopped by user; saved work can be resumed")
                emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
                _emit_finished(cfg, client, report)
                return report
            except Exception as e:
                # never change the exception type: callers and tests match on
                # it. Carry the partial report so the CLI can still report the
                # branch, the worktree and any ticket already committed.
                _record_partial(cfg, store, report)
                e.report = report
                raise
            finally:
                _record_usage(client, state_dir, issue_ref, report)
    finally:
        cfg.repo_dir = original_dir
        if client_config is not None:
            client_config.repo_dir = original_client_dir


def _record_partial(cfg: RunnerConfig, store: TicketStore, report: RunReport) -> None:
    """Fill a report with whatever the run achieved before it failed."""
    report.done = sum(t.status == "done" for t in store.tickets)
    report.blocked = sum(t.status == "blocked" for t in store.tickets)
    for ticket in store.tickets:
        if ticket.status == "done" and ticket.commit_sha:
            detail = f"ticket {ticket.id} done @ {ticket.commit_sha[:12]}: {ticket.title}"
            if detail not in report.details:
                report.details.append(detail)
    emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))


def _prepare_workspace(
    cfg: RunnerConfig, store: TicketStore, source: Path, branch_slug: str
) -> None:
    for pattern in devops.DEFAULT_EXCLUDES:
        devops.ensure_excluded(source, pattern)
    if store.state_dir.is_relative_to(source):
        relative = store.state_dir.relative_to(source).as_posix()
        if relative == ".":
            raise StateError("runner state must not be stored at the repository root")
        devops.ensure_excluded(source, f"/{relative}/")

    if store.worktree:
        workspace = Path(store.worktree).resolve()
        if workspace != source and not workspace.is_relative_to(store.state_dir / "worktrees"):
            raise StateError("saved worktree is outside the runner-owned workspace directory")
        if not store.branch:
            raise StateError("saved worktree has no branch")
        if not store.workspace_ready:
            if not store.initial_head:
                raise StateError("pending workspace has no recorded base commit")
            if workspace == source:
                if devops.current_branch(source) != store.branch:
                    devops.create_branch(source, store.issue_ref, branch_slug)
            else:
                devops.finish_worktree_creation(source, store.branch, workspace, store.initial_head)
            store.workspace_ready = True
            store.save()
        if (
            not workspace.is_dir()
            or devops.git_common_dir(workspace) != devops.git_common_dir(source)
            or devops.current_branch(workspace) != store.branch
        ):
            raise DevopsError("saved run worktree or branch is missing or was changed")
        if workspace != source:
            devops.require_clean(source)
        cfg.repo_dir = workspace
        return

    # Legacy state can resume in its original clean issue checkout, never by guessing
    # ownership of pre-existing dirty files.
    devops.require_clean(source)
    if store.branch:
        if devops.current_branch(source) != store.branch:
            raise StateError(
                f"legacy state expects branch {store.branch}; resume in its clean checkout"
            )
        store.worktree = str(source)
        store.workspace_ready = True
        store.initial_head = devops.head_commit(source)
        store.last_commit = store.initial_head
        store.save()
        return

    store.branch = (
        f"issue-{store.issue_ref}-{branch_slug}" if branch_slug else f"issue-{store.issue_ref}"
    )
    if devops.branch_exists(source, store.branch):
        raise DevopsError(
            f"branch {store.branch} exists without matching run state; refusing to reuse it"
        )
    store.initial_head = devops.head_commit(source)
    store.last_commit = store.initial_head
    workspace = store.state_dir / "worktrees" / store.issue_ref if cfg.isolate_worktree else source
    store.worktree = str(workspace)
    store.save()
    if cfg.isolate_worktree:
        devops.create_worktree(source, store.branch, workspace)
    else:
        devops.create_branch(source, store.issue_ref, branch_slug)
    store.workspace_ready = True
    store.save()
    cfg.repo_dir = workspace


def _run_issue(
    cfg: RunnerConfig,
    client,
    issue: dict,
    store: TicketStore,
    report: RunReport,
    plan_only: bool,
) -> RunReport:
    issue_ref = store.issue_ref
    if report.worktree:
        log.info("run workspace: %s", report.worktree)
    emit(cfg.events, "run_started", issue_ref=issue_ref, title=issue["title"])
    emit(cfg.events, "phase", name="plan")
    cfg.control.check()

    if store.tickets:
        log.info("resuming: %d tickets loaded from %s", len(store.tickets), store.state_file)
    else:
        _render_visual(cfg, plan="pending", branch="pending", tickets=[])
        try:
            summary, tickets = plan_step(client, cfg, issue)
        except BudgetExhausted as e:
            report.budget_exhausted = True
            report.details.append(f"planning did not start: {e}")
            _emit_finished(cfg, client, report)
            return report
        store.plan_summary = summary
        store.set_tickets(tickets)
        store.save()
        _render_visual(cfg, plan="done", branch="pending", tickets=store.tickets)
        log.info("plan: %d tickets — %s", len(tickets), summary)
    emit(
        cfg.events,
        "tickets_updated",
        tickets=ticket_snapshot(store.tickets),
        summary=store.plan_summary,
    )
    cfg.control.check()

    # mirror tickets to the tracker; also backfills runs planned without a backend
    if cfg.tickets_backend and issue["number"]:
        for ticket in store.tickets:
            cfg.control.check()
            if ticket.github_issue is None:
                ticket.github_issue = cfg.tickets_backend.create(issue["number"], ticket)
                store.save()
                if ticket.status == "done":
                    cfg.tickets_backend.close(ticket, "completed in an earlier run")
        store.save()

    report.done = sum(1 for t in store.tickets if t.status == "done")
    report.blocked = sum(1 for t in store.tickets if t.status == "blocked")
    report.pr_url = store.pr_url
    if plan_only:
        report.details.append("plan-only run; no tickets executed")
        for t in store.tickets:
            report.details.append(
                f"ticket {t.id} [{t.status}]: {t.title} — assert: {t.test_assertion}"
            )
        _emit_finished(cfg, client, report)
        return report

    if cfg.retry_blocked:
        for ticket in store.tickets:
            if ticket.status == "blocked":
                ticket.status = "pending"
                ticket.rounds = 0
                ticket.blocked_reason = None
                report.blocked -= 1
        store.save()

    emit(cfg.events, "phase", name="branch")
    _render_visual(cfg, plan="done", branch=store.branch, tickets=store.tickets)

    emit(cfg.events, "phase", name="build")
    while True:
        cfg.control.check()
        ready = store.ready()
        if not ready:
            break
        unfinished = [t for t in store.tickets if t.base_commit and t.status != "done"]
        active = [t for t in unfinished if t.status != "blocked"]
        if len(unfinished) > 1:
            raise StateError("multiple tickets own unfinished changes; refusing ambiguous recovery")
        if unfinished and unfinished[0].status == "blocked" and devops.changed_paths(cfg.repo_dir):
            report.details.append(
                f"work retained in {cfg.repo_dir}; use --retry-blocked before starting more tickets"
            )
            break
        next_ticket = active[0] if active else ready[0]
        if next_ticket not in ready:
            raise StateError("an unfinished ticket has unsatisfied dependencies")
        _process_ticket(cfg, client, store, next_ticket, report)
        if report.budget_exhausted:
            log.warning("run stopped: credit budget exhausted; re-run to resume")
            report.details.append("run stopped early: credit budget exhausted (state is resumable)")
            break

    if not report.budget_exhausted and not store.ready():
        _block_unsatisfiable(cfg, store, report)

    if report.done and not report.blocked and not store.pending() and not report.budget_exhausted:
        devops.require_clean(cfg.repo_dir)
        if store.last_commit and devops.head_commit(cfg.repo_dir) != store.last_commit:
            raise DevopsError(
                "run branch moved after its last accepted commit; refusing publication"
            )
        approved_head = devops.head_commit(cfg.repo_dir)
        _regression_gate(cfg)
        cfg.control.check()
        devops.require_clean(cfg.repo_dir)
        if (
            devops.head_commit(cfg.repo_dir) != approved_head
            or devops.current_branch(cfg.repo_dir) != store.branch
        ):
            raise DevopsError("regression command changed the run branch; refusing publication")
        _open_pull_request(cfg, issue, store, report)
    _emit_finished(cfg, client, report)
    return report


def _pr_body(issue: dict, store: TicketStore, report: RunReport) -> str:
    lines = [f"Closes #{issue['number']}", ""]
    if store.plan_summary:
        lines += [store.plan_summary, ""]
    lines.append("### Tickets")
    for ticket in store.tickets:
        if ticket.status == "done":
            lines.append(f"- {ticket.id}. {ticket.title} — asserts `{ticket.test_assertion}`")
    lines += ["", f"Branch `{report.branch}`, opened by issue-runner. Co-authored with AI."]
    return "\n".join(lines)


def _open_pull_request(
    cfg: RunnerConfig, issue: dict, store: TicketStore, report: RunReport
) -> None:
    """Publish the branch and open a PR — only for a clean run on a known GitHub repo.

    A failure here never fails the run: the commits are already on the branch, so
    the reason is recorded in the report and the user can open the PR by hand.
    """
    if not cfg.open_pr or not cfg.repo or not issue["number"] or store.pr_url:
        return
    if report.blocked or not report.done or report.budget_exhausted:
        return
    title = f"Fixes #{issue['number']} — {issue['title']}"
    try:
        cfg.control.check()
        devops.push_branch(cfg.repo_dir, report.branch)
        cfg.control.check()
        report.pr_url = github_io.open_pull_request(
            cfg.repo, head=report.branch, title=title, body=_pr_body(issue, store, report)
        )
        store.pr_url = report.pr_url
        store.save()
    except (DevopsError, GithubError) as e:
        log.warning("pull request not opened: %s", e)
        report.details.append(f"pull request not opened: {e}")
        return
    report.details.append(f"pull request opened: {report.pr_url}")
    emit(cfg.events, "pull_request_opened", url=report.pr_url)


def _record_usage(client, state_dir: Path, issue_ref: str, report: RunReport) -> None:
    """Persist per-run accounting. Optional: fakes and stubs carry no ledger."""
    ledger = getattr(client, "usage", None)
    if ledger is None:
        return
    report.usage_summary = ledger.summary_line()
    try:
        ledger.save(state_dir, issue_ref)
    except OSError as e:
        log.warning("could not write the usage file: %s", e)


def _emit_finished(cfg: RunnerConfig, client, report: RunReport) -> None:
    announce_stop(cfg)
    ledger = getattr(client, "usage", None)
    if ledger is not None:
        report.usage_summary = ledger.summary_line()
    budget = getattr(client, "budget", None)
    if isinstance(budget, RunBudget) and (budget.limit is not None or not budget.cost_is_complete):
        report.budget_summary = budget.describe()
    emit(cfg.events, "phase", name="finished")
    emit(
        cfg.events,
        "run_finished",
        done=report.done,
        blocked=report.blocked,
        branch=report.branch,
        worktree=report.worktree,
        pr_url=report.pr_url,
        usage=report.usage_summary,
        budget=report.budget_summary,
        budget_exhausted=report.budget_exhausted,
        plan_only=report.plan_only,
        stopped=report.stopped,
    )


def _block_unsatisfiable(cfg: RunnerConfig, store: TicketStore, report: RunReport) -> None:
    """Nothing is ready but tickets remain: their dependencies can never be met.

    Blocking each one explicitly (rather than leaving it pending) makes the
    failure visible in the report and the tracker, and keeps the run terminating.
    Tickets with a nameable cause are blocked first so a dependent cites the
    prerequisite that actually failed; only what is left over is a true cycle.
    """
    while True:
        remaining = store.pending()
        if not remaining:
            return
        nameable = [(t, store.dependency_failure(t)) for t in remaining]
        nameable = [(t, reason) for t, reason in nameable if reason]
        if not nameable:
            # a true cycle has no root cause; label every member the same way
            reasons = [(t, store.unsatisfiable_reason(t)) for t in remaining]
            for ticket, reason in reasons:
                _block(store, ticket, report, reason, cfg)
            return
        for ticket, reason in nameable:
            _block(store, ticket, report, reason, cfg)


def _process_ticket(
    cfg: RunnerConfig,
    client,
    store: TicketStore,
    ticket: Ticket,
    report: RunReport,
    journal: Journal | None = None,
) -> None:
    journal = journal or Journal(cfg.tickets_backend)
    if ticket.base_commit is None:
        devops.require_clean(cfg.repo_dir)
        head = devops.head_commit(cfg.repo_dir)
        if store.last_commit and head != store.last_commit:
            raise DevopsError(
                "run branch moved outside the runner; refusing to adopt unrelated work"
            )
        ticket.base_commit = head
        ticket.commit_token = uuid4().hex
    ticket.status = "in_progress"
    store.save()
    log.info("ticket %d: %s", ticket.id, ticket.title)
    emit(cfg.events, "ticket_started", ticket_id=ticket.id, title=ticket.title)
    _mark_in_progress(cfg, ticket)
    if ticket.phase == "tester" and ticket.rounds == 0:
        # only on the ticket's first start: a resumed run must not repost the brief,
        # since the in-process duplicate guard is gone once the process restarts
        journal.post(ticket, "planner", "builder.tester", _brief_body(ticket, store), "brief")
    emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
    try:
        while True:
            cfg.control.check()
            if devops.current_branch(cfg.repo_dir) != store.branch:
                raise DevopsError("the agent changed the run branch")
            if ticket.phase == "commit":
                recovered = devops.recover_commit(cfg.repo_dir, ticket)
                if recovered is not None:
                    _finish_ticket(cfg, store, ticket, report, recovered)
                    return
            if devops.head_commit(cfg.repo_dir) != ticket.base_commit:
                raise DevopsError("the agent changed HEAD outside the commit step")

            if ticket.phase in ("tester", "refine_test"):
                refining = ticket.phase == "refine_test"
                ticket.already_satisfied = False
                try:
                    path = tester_step(
                        client,
                        cfg,
                        ticket,
                        feedback=ticket.test_feedback or None,
                        require_red=not refining,
                        journal=journal,
                    )
                except TestAlreadyPasses as e:
                    path = e.test_path
                    ticket.already_satisfied = True
                _snapshot_test(cfg, ticket, path)
                ticket.phase = "verifier" if ticket.already_satisfied else "coder"
                journal.post(
                    ticket,
                    "builder.tester",
                    "verifier" if ticket.already_satisfied else "builder.coder",
                    _test_body(ticket, path),
                    "test accepted",
                )
                ticket.code_feedback = ""
                ticket.test_feedback = ""
                store.save()
                continue

            test_path = _require_accepted_test(cfg, ticket)
            if ticket.phase == "coder":
                passed, _ = run_tests(cfg, test_path)
                if ticket.code_feedback or not passed:
                    try:
                        coder_step(
                            client,
                            cfg,
                            ticket,
                            test_path,
                            feedback=ticket.code_feedback or None,
                            journal=journal,
                        )
                    except CoderFailure as e:
                        # the spec may be the problem: let the tester repair it
                        # rather than losing the ticket to an unsatisfiable test
                        cfg.control.check()
                        ticket.test_feedback = str(e)
                        ticket.code_feedback = ""
                        ticket.phase = "refine_test"
                        journal.post(
                            ticket,
                            "builder.coder",
                            "builder.tester",
                            str(e),
                            "coder exhausted its retries — the spec may be wrong",
                        )
                        if not _hand_back(cfg, store, ticket, report, str(e)):
                            return
                        continue
                _require_accepted_test(cfg, ticket)
                ticket.phase = "verifier"
                ticket.code_feedback = ""
                journal.post(
                    ticket,
                    "builder.coder",
                    "verifier",
                    _code_body(cfg, test_path),
                    "implementation ready for review",
                )
                store.save()
                continue

            if ticket.phase == "verifier":
                before = devops.workspace_digest(cfg.repo_dir)
                verdict = verify_step(client, cfg, ticket, test_path, journal=journal)
                if devops.workspace_digest(cfg.repo_dir) != before:
                    raise BuildError("read-only verifier modified the workspace; changes retained")
                log.info(
                    "ticket %d verdict: %s (%s)",
                    ticket.id,
                    verdict.verdict,
                    "; ".join(verdict.reasons),
                )
                emit(
                    cfg.events,
                    "verdict",
                    ticket_id=ticket.id,
                    verdict=verdict.verdict,
                    reasons=verdict.reasons,
                )
                if verdict.verdict == "pass":
                    ticket.approved_digest = before
                    ticket.phase = "regression"
                    journal.post(
                        ticket,
                        "verifier",
                        "harness",
                        _verdict_body(verdict),
                        "pass — going to the regression gate",
                    )
                    store.save()
                    continue
                ticket.test_feedback = verdict.test_feedback
                ticket.code_feedback = verdict.code_feedback
                ticket.phase = "refine_test" if verdict.verdict == "refine_test" else "coder"
                journal.post(
                    ticket,
                    "verifier",
                    "builder.tester" if verdict.verdict == "refine_test" else "builder.coder",
                    _verdict_body(verdict),
                    verdict.verdict,
                )
                if not _hand_back(cfg, store, ticket, report, f"last verdict {verdict.verdict}"):
                    return
                continue

            if ticket.phase == "regression":
                if devops.workspace_digest(cfg.repo_dir) != ticket.approved_digest:
                    raise BuildError("workspace changed after verification; refusing approval")
                try:
                    _regression_gate(cfg)
                except RegressionFailure as e:
                    cfg.control.check()
                    ticket.code_feedback = str(e)
                    ticket.phase = "coder"
                    journal.post(
                        ticket, "harness", "builder.coder", str(e), "regression suite failed"
                    )
                    if not _hand_back(cfg, store, ticket, report, str(e)):
                        return
                    continue
                if devops.workspace_digest(cfg.repo_dir) != ticket.approved_digest:
                    raise BuildError("regression command modified the workspace; changes retained")
                devops.approve_changes(cfg.repo_dir, ticket)
                ticket.phase = "commit"
                store.save()
                continue

            if ticket.phase == "commit":
                sha = devops.commit_ticket(cfg.repo_dir, ticket, expected_branch=store.branch)
                _finish_ticket(cfg, store, ticket, report, sha)
                return
    except BudgetExhausted as e:
        report.budget_exhausted = True
        ticket.status = "pending"
        store.save()
        report.details.append(f"ticket {ticket.id} paused before {ticket.phase}: {e}")
        emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
    except (BuildError, CopilotError, VerifyError, DevopsError) as e:
        cfg.control.check()
        _block(store, ticket, report, str(e), cfg)


def _mark_in_progress(cfg: RunnerConfig, ticket: Ticket) -> None:
    """Show on the tracker that this sub-task is being worked on right now.

    Best-effort by design: this is a status light, and a tracker that will not
    take it is no reason to refuse to do the work.
    """
    backend = cfg.tickets_backend
    if not ticket.github_issue or backend is None or not hasattr(backend, "start"):
        return
    try:
        backend.start(ticket)
    except Exception:  # noqa: BLE001 — a status light must never stop the run
        log.warning("could not mark ticket %s in progress", ticket.id, exc_info=True)


def _brief_body(ticket: Ticket, store: TicketStore) -> str:
    """What the planner asked for — the brief every later role is judged against."""
    parts = [ticket.description.strip()]
    parts.append(f"**Single test assertion:** `{ticket.test_assertion}`")
    if ticket.files_hint:
        parts.append("**Files likely involved:** " + ", ".join(ticket.files_hint))
    if ticket.depends_on:
        parts.append("**Depends on:** " + ", ".join(f"ticket {d}" for d in ticket.depends_on))
    if store.branch:
        parts.append(f"Work happens on `{store.branch}`.")
    return "\n\n".join(p for p in parts if p)


def _test_body(ticket: Ticket, path: str) -> str:
    """The specification the coder must satisfy, and the proof it is a real one."""
    if ticket.already_satisfied:
        return (
            f"Accepted test: `{path}`\n\n"
            "It passes with no new code, so the behaviour may already exist. "
            "Sent straight to the verifier to arbitrate rather than to the coder."
        )
    return (
        f"Accepted test: `{path}`\n\n"
        "It was executed and observed to FAIL before any implementation exists, which is "
        "what makes it a specification rather than a claim. It is now frozen: the coder "
        "cannot edit it."
    )


def _code_body(cfg: RunnerConfig, test_path: str) -> str:
    """What the coder changed to turn the frozen test green."""
    try:
        changed = [p for p in devops.changed_paths(cfg.repo_dir) if p != test_path]
    except DevopsError:
        # this text is a record, never a gate: failing to list the files must
        # not cost a ticket that has already been proven green
        changed = []
    files = "\n".join(f"- `{p}`" for p in changed) if changed else "- (no source file changed)"
    return f"The frozen test `{test_path}` now passes.\n\nChanged:\n{files}"


def _verdict_body(verdict) -> str:
    """The verifier's reasoning, in the form the next agent has to act on."""
    parts = [f"Verdict: **{verdict.verdict}**"]
    if verdict.reasons:
        parts.append("\n".join(f"- {r}" for r in verdict.reasons))
    if verdict.test_feedback:
        parts.append(f"**For builder.tester:**\n{verdict.test_feedback}")
    if verdict.code_feedback:
        parts.append(f"**For builder.coder:**\n{verdict.code_feedback}")
    return "\n\n".join(parts)


def _snapshot_test(cfg: RunnerConfig, ticket: Ticket, path: str) -> None:
    try:
        content = resolve_test_path(cfg, path).read_bytes()
    except OSError as e:
        raise BuildError(f"cannot snapshot accepted test {path}: {e}") from e
    ticket.test_path = path
    ticket.test_snapshot = base64.b64encode(content).decode("ascii")
    ticket.test_hash = hashlib.sha256(content).hexdigest()


def _require_accepted_test(cfg: RunnerConfig, ticket: Ticket) -> str:
    if ticket.test_path is None or ticket.test_snapshot is None or ticket.test_hash is None:
        raise StateError(f"ticket {ticket.id} has no accepted test snapshot for {ticket.phase}")
    try:
        original = base64.b64decode(ticket.test_snapshot, validate=True)
    except ValueError as e:
        raise StateError(f"ticket {ticket.id} has an invalid test snapshot") from e
    if hashlib.sha256(original).hexdigest() != ticket.test_hash:
        raise StateError(f"ticket {ticket.id} test snapshot checksum is invalid")
    path = resolve_test_path(cfg, ticket.test_path)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ticket.test_hash:
        raise BuildError(
            f"accepted test {ticket.test_path} changed outside the tester phase; "
            "no files were overwritten; restore the accepted artifact before retrying"
        )
    return ticket.test_path


def _regression_gate(cfg: RunnerConfig) -> None:
    cfg.control.check()
    command = cfg.regression_cmd or detect_regression_cmd(cfg.repo_dir)
    if not command:
        raise BuildError(
            "cannot detect a full regression command; set regression_cmd or --regression-cmd"
        )
    if "{test_path}" in command:
        raise BuildError("regression_cmd must run the suite without a {test_path} selector")
    try:
        passed, output = run_test_command(cfg, command)
    except BuildError:
        cfg.control.check()
        raise
    cfg.control.check()
    if not passed:
        raise RegressionFailure(
            f"regression gate failed; repair existing behaviour:\n{output[-3000:]}"
        )


def _hand_back(
    cfg: RunnerConfig, store: TicketStore, ticket: Ticket, report: RunReport, reason: str
) -> bool:
    ticket.rounds += 1
    store.save()
    if ticket.rounds > cfg.max_rounds:
        _block(store, ticket, report, f"exceeded max_rounds={cfg.max_rounds}; {reason}", cfg)
        return False
    return True


def _finish_ticket(
    cfg: RunnerConfig, store: TicketStore, ticket: Ticket, report: RunReport, sha: str
) -> None:
    ticket.commit_sha = sha
    ticket.status = "done"
    ticket.blocked_reason = None
    store.last_commit = sha
    store.save()
    report.done += 1
    note = "already satisfied by existing code; " if ticket.already_satisfied else ""
    report.details.append(f"ticket {ticket.id} done @ {sha[:12]}: {note}{ticket.title}")
    emit(cfg.events, "ticket_done", ticket_id=ticket.id, note=f"committed {sha[:12]}")
    emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
    # deliberately the last thing that happens to this ticket: the work is
    # committed and proven, so the tracker is told it is done immediately
    # before the runner moves on to the next one
    if ticket.github_issue and cfg.tickets_backend:
        try:
            cfg.tickets_backend.close(ticket, f"{note}Done in {sha} on {store.branch}")
        except Exception:  # noqa: BLE001 — the commit already happened
            log.warning("could not close the sub-issue for ticket %s", ticket.id, exc_info=True)
            report.details.append(
                f"ticket {ticket.id} is committed at {sha[:12]} but its sub-issue "
                f"#{ticket.github_issue} could not be closed"
            )


def _block(
    store: TicketStore, ticket: Ticket, report: RunReport, reason: str, cfg: RunnerConfig = None
) -> None:
    ticket.status = "blocked"
    ticket.blocked_reason = reason
    if cfg is not None and ticket.base_commit and not devops.changed_paths(cfg.repo_dir):
        ticket.base_commit = None
    store.save()
    report.blocked += 1
    report.details.append(f"ticket {ticket.id} BLOCKED: {reason}")
    log.warning("ticket %d blocked: %s", ticket.id, reason)
    if cfg:
        emit(cfg.events, "ticket_blocked", ticket_id=ticket.id, reason=reason)
        emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
    backend = cfg.tickets_backend if cfg else None
    if ticket.github_issue and backend and hasattr(backend, "block"):
        backend.block(ticket, reason)
