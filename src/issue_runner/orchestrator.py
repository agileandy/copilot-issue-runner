"""Orchestrator: wires plan -> branch -> per-ticket build/verify loop.

Routing per ticket (Andy's spec):
  builder.tester (red enforced) -> builder.coder (green enforced) -> verifier
    refine_test -> back to tester (red not required: code exists) -> coder if red
    rework_code -> back to coder
    pass        -> devops commit, ticket done
Hand-backs are capped by max_rounds; a non-converging ticket is marked blocked
and the run continues. State is saved after every transition for resume.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import RunnerConfig
from .copilot import CopilotError
from .events import emit, ticket_snapshot
from .phases import devops
from .phases.build import (
    BuildError,
    TestAlreadyPasses,
    coder_step,
    run_tests,
    tester_step,
)
from .phases.devops import DevopsError
from .phases.plan import plan_step
from .phases.verify import VerifyError, verify_step
from .tickets import Ticket, TicketStore
from .visual import render_flow

log = logging.getLogger("issue_runner")


@dataclass
class RunReport:
    branch: str = ""
    done: int = 0
    blocked: int = 0
    details: list[str] = field(default_factory=list)


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
    if issue["number"]:
        issue_ref, branch_slug = str(issue["number"]), devops.slugify(issue["title"])
    else:
        issue_ref, branch_slug = devops.slugify(issue["title"], 20), ""
    state_dir = state_dir or Path(cfg.repo_dir) / ".issue-runner"
    store = TicketStore(state_dir, issue_ref=issue_ref)
    emit(cfg.events, "run_started", issue_ref=issue_ref, title=issue["title"])
    emit(cfg.events, "phase", name="plan")

    # Phase 1: plan (skipped on resume — the saved plan is the contract)
    if store.load():
        log.info("resuming: %d tickets loaded from %s", len(store.tickets), store.state_file)
    else:
        _render_visual(cfg, plan="pending", branch="pending", tickets=[])
        summary, tickets = plan_step(client, cfg, issue)
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

    # mirror tickets to the tracker; also backfills runs planned without a backend
    if cfg.tickets_backend and issue["number"]:
        for ticket in store.tickets:
            if ticket.github_issue is None:
                ticket.github_issue = cfg.tickets_backend.create(issue["number"], ticket)
                if ticket.status == "done":
                    cfg.tickets_backend.close(ticket, "completed in an earlier run")
        store.save()

    report = RunReport(
        done=sum(1 for t in store.tickets if t.status == "done"),
        blocked=sum(1 for t in store.tickets if t.status == "blocked"),
    )
    if plan_only:
        report.details.append("plan-only run; no tickets executed")
        for t in store.tickets:
            report.details.append(
                f"ticket {t.id} [{t.status}]: {t.title} — assert: {t.test_assertion}"
            )
        emit(cfg.events, "phase", name="finished")
        emit(cfg.events, "run_finished", done=report.done, blocked=report.blocked, branch="")
        return report

    if cfg.retry_blocked:
        for ticket in store.tickets:
            if ticket.status == "blocked":
                ticket.status = "pending"
                ticket.rounds = 0
                ticket.blocked_reason = None
                report.blocked -= 1
        store.save()

    # Phase 2: branch
    emit(cfg.events, "phase", name="branch")
    _render_visual(cfg, plan="done", branch="pending", tickets=store.tickets)
    store.branch = devops.create_branch(cfg.repo_dir, issue_ref, branch_slug)
    store.save()
    for pattern in devops.DEFAULT_EXCLUDES:
        devops.ensure_excluded(cfg.repo_dir, pattern)
    report.branch = store.branch
    _render_visual(cfg, plan="done", branch=store.branch, tickets=store.tickets)

    # Phase 3: build/verify loop
    emit(cfg.events, "phase", name="build")
    for ticket in store.pending():
        _process_ticket(cfg, client, store, ticket, report)

    emit(cfg.events, "phase", name="finished")
    emit(cfg.events, "run_finished", done=report.done, blocked=report.blocked, branch=report.branch)
    return report


def _process_ticket(
    cfg: RunnerConfig, client, store: TicketStore, ticket: Ticket, report: RunReport
) -> None:
    ticket.status = "in_progress"
    store.save()
    log.info("ticket %d: %s", ticket.id, ticket.title)
    emit(cfg.events, "ticket_started", ticket_id=ticket.id, title=ticket.title)
    emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
    try:
        try:
            test_path = tester_step(client, cfg, ticket)
        except TestAlreadyPasses as e:
            _arbitrate_already_satisfied(cfg, client, store, ticket, e, report)
            return
        ticket.test_path = test_path
        store.save()
        coder_step(client, cfg, ticket, test_path)

        while True:
            verdict = verify_step(client, cfg, ticket, test_path)
            log.info(
                "ticket %d verdict: %s (%s)", ticket.id, verdict.verdict, "; ".join(verdict.reasons)
            )
            emit(
                cfg.events,
                "verdict",
                ticket_id=ticket.id,
                verdict=verdict.verdict,
                reasons=verdict.reasons,
            )
            if verdict.verdict == "pass":
                sha = devops.commit_ticket(cfg.repo_dir, ticket)
                ticket.status = "done"
                store.save()
                report.done += 1
                report.details.append(f"ticket {ticket.id} done @ {sha}: {ticket.title}")
                emit(cfg.events, "ticket_done", ticket_id=ticket.id, note=f"committed {sha}")
                emit(cfg.events, "tickets_updated", tickets=ticket_snapshot(store.tickets))
                if ticket.github_issue and cfg.tickets_backend:
                    cfg.tickets_backend.close(ticket, f"Done in {sha} on {store.branch}")
                return

            ticket.rounds += 1
            store.save()
            if ticket.rounds > cfg.max_rounds:
                _block(
                    store,
                    ticket,
                    report,
                    f"exceeded max_rounds={cfg.max_rounds}; last verdict {verdict.verdict}",
                    cfg,
                )
                return

            if verdict.verdict == "refine_test":
                test_path = tester_step(
                    client, cfg, ticket, feedback=verdict.test_feedback, require_red=False
                )
                ticket.test_path = test_path
                store.save()
                passed, _ = run_tests(cfg, test_path)
                if not passed:
                    coder_step(
                        client,
                        cfg,
                        ticket,
                        test_path,
                        feedback="the refined test fails; make it pass",
                    )
            else:  # rework_code
                coder_step(client, cfg, ticket, test_path, feedback=verdict.code_feedback)
    except (BuildError, CopilotError, VerifyError, DevopsError) as e:
        # contain the failure to this ticket; the run (and its state) continues
        _block(store, ticket, report, str(e), cfg)


def _arbitrate_already_satisfied(
    cfg: RunnerConfig, client, store: TicketStore, ticket: Ticket, e, report: RunReport
) -> None:
    """Every candidate test passed without new code: let the verifier rule.

    pass -> the behaviour exists and the test is robust; commit the test as the
    ticket's regression artifact and mark done. Anything else -> blocked, with
    the verifier's reasoning recorded (and mirrored to the tracker).
    """
    log.info("ticket %d: candidate tests keep passing — asking verifier to arbitrate", ticket.id)
    verdict = verify_step(client, cfg, ticket, e.test_path)
    if verdict.verdict == "pass":
        ticket.test_path = e.test_path
        try:
            sha = devops.commit_ticket(cfg.repo_dir, ticket)
        except DevopsError as de:
            if "nothing to commit" not in str(de):
                raise
            sha = "(no new files)"
        ticket.status = "done"
        store.save()
        report.done += 1
        note = f"already satisfied by existing code — verifier confirmed; regression test @ {sha}"
        report.details.append(f"ticket {ticket.id} done: {note}")
        if ticket.github_issue and cfg.tickets_backend:
            cfg.tickets_backend.close(ticket, note)
        return
    reason = (
        "behaviour appears pre-existing but the verifier refused to confirm "
        f"({verdict.verdict}): {'; '.join(verdict.reasons)} "
        f"{verdict.test_feedback or verdict.code_feedback}".strip()
    )
    _block(store, ticket, report, reason, cfg)


def _block(
    store: TicketStore, ticket: Ticket, report: RunReport, reason: str, cfg: RunnerConfig = None
) -> None:
    ticket.status = "blocked"
    ticket.blocked_reason = reason
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
