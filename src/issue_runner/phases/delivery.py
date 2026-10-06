"""Delivery for --deploy: from built tickets to a verified Dev deployment.

The Definition of Done is six gates (tickets.DOD_GATES). The run succeeds only
when every gate passes; anything else is a failure that names the gate it
stopped at. Each stage saves state before and after its work, so a stopped or
crashed run resumes at the same stage, and a failed stage is retried by
re-running the same command.
"""

import logging
import time

from ..budget import BudgetExhausted
from ..config import RunnerConfig
from ..copilot import CopilotError
from ..events import emit
from ..github_flow import GithubError, GitHubFlow
from ..tickets import Delivery, Ticket, TicketStore
from . import acceptance, devops, merge, review
from .devops import DevopsError
from .plan import PlanError, plan_extra

log = logging.getLogger("issue_runner")

# the clock and the GitHub client are module seams so tests can drive them
_now = time.time
_sleep = time.sleep


def make_flow(cfg: RunnerConfig) -> GitHubFlow:
    return GitHubFlow(cfg.preflight.repo)


# exit code per failed gate; tickets keeps the existing 1/3/4 codes
GATE_EXIT = {"criteria_tests": 5, "review": 6, "merge": 7, "deploy": 8, "criteria_dev": 5}

# the gate each stage decides; a stage that fails records this gate
STAGE_GATE = {
    "built": "criteria_tests",
    "dev_checks": "criteria_dev",
    "accepted": "review",
    "reviewing": "review",
    "revising": "review",
    "merging": "merge",
    "deploying": "deploy",
    "verifying": "criteria_dev",
}


MORE_TICKETS = "more_tickets"


class GateFailed(Exception):
    """A delivery stage could not pass its gate. The message is the evidence."""


class MoreTickets(Exception):
    """The stage added tickets; the build loop must run them, then delivery resumes."""


def criteria_records(criteria, where: dict[str, str], tickets: list[Ticket]) -> list[dict]:
    """The saved form of each criterion: where it is proven and which tickets prove it."""
    return [
        {
            "id": c.id,
            "text": c.text,
            "where": where[c.id],
            "tickets": [t.id for t in tickets if c.id in t.criteria],
            "pre_merge": None,
            "dev": None,
            "evidence": [],
            "reason": "",
        }
        for c in criteria
    ]


def run(cfg: RunnerConfig, client, issue: dict, store: TicketStore, report) -> str | None:
    """Advance the delivery until it is done or a gate fails.

    Returns MORE_TICKETS when a stage planned new tickets for the build loop.
    """
    delivery = store.delivery or Delivery()
    store.delivery = delivery
    delivery.gates["tickets"] = "pass"
    if delivery.failed_gate:
        log.info("retrying %s after: %s", delivery.stage, delivery.failed_reason)
        delivery.gates[delivery.failed_gate] = None
        delivery.failed_gate = delivery.failed_reason = None
    store.save()
    report.deploy = True

    while delivery.stage != "done":
        cfg.control.check()
        stage = delivery.stage
        emit(cfg.events, "phase", name=stage)
        handler = _HANDLERS[stage]
        try:
            handler(cfg, client, issue, store, delivery)
        except MoreTickets:
            store.save()
            return MORE_TICKETS
        except BudgetExhausted as e:
            store.save()
            report.budget_exhausted = True
            report.details.append(f"delivery paused at {stage}: {e} (state is resumable)")
            return
        except GateFailed as e:
            gate = STAGE_GATE[stage]
            delivery.gates[gate] = "fail"
            delivery.failed_gate = gate
            delivery.failed_reason = str(e)
            store.save()
            report.dod_failed_gate = gate
            report.details.append(f"definition of done FAILED at {gate}: {e}")
            emit(cfg.events, "gate", name=gate, result="fail", reason=str(e))
            return None
        store.save()
    report.dod_met = True
    report.details.append("definition of done met: every gate passed")
    return None


def _pass(cfg, delivery: Delivery, gate: str) -> None:
    delivery.gates[gate] = "pass"
    emit(cfg.events, "gate", name=gate, result="pass")


def _built(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """criteria_tests: every criterion proven by tests is met, with passing tests cited."""
    if not delivery.criteria:
        raise GateFailed(
            "this run was planned without acceptance criteria; start a fresh --deploy run"
        )
    tested = [c for c in delivery.criteria if c["where"] == "tests"]
    if tested:
        try:
            verdicts = acceptance.judge(client, cfg, issue, store, tested)
        except (acceptance.AcceptError, CopilotError) as e:
            raise GateFailed(str(e)) from e
        by_id = {v["id"]: v for v in verdicts}
        for c in tested:
            v = by_id[c["id"]]
            c["pre_merge"], c["evidence"], c["reason"] = v["verdict"], v["tests"], v["reason"]
        store.save()
        unmet = [c for c in tested if c["pre_merge"] != "met"]
        if unmet:
            if delivery.acceptance_round < cfg.deploy_settings.max_acceptance_rounds:
                _add_tickets_for(cfg, client, issue, store, delivery, unmet)
                raise MoreTickets
            raise GateFailed(
                "acceptance criteria not met: "
                + "; ".join(f"{c['id']} ({c['reason']})" for c in unmet)
            )
    _pass(cfg, delivery, "criteria_tests")
    delivery.stage = "dev_checks"


def _dev_checks(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """Write a failing-first Dev check for every criterion only Dev can show."""
    problem = acceptance.harness_problem(cfg, delivery.criteria)
    if problem:
        raise GateFailed(problem)
    for c in delivery.criteria:
        if c["where"] != "dev" or c.get("check_hash"):
            continue
        cfg.control.check()
        try:
            c.update(acceptance.write_red_check(client, cfg, issue, store, c))
        except (acceptance.AcceptError, CopilotError) as e:
            raise GateFailed(str(e)) from e
        store.save()
    delivery.stage = "accepted"


def _add_tickets_for(cfg, client, issue, store, delivery: Delivery, unmet: list[dict]) -> None:
    try:
        summary, tickets = plan_extra(
            client, cfg, issue, unmet, store.tickets, [c["id"] for c in delivery.criteria]
        )
    except (PlanError, CopilotError) as e:
        raise GateFailed(f"could not plan tickets for the unmet criteria: {e}") from e
    store.tickets.extend(tickets)
    delivery.acceptance_round += 1
    for c in delivery.criteria:
        c["tickets"] += [t.id for t in tickets if c["id"] in t.criteria]
    log.info(
        "acceptance round %d: %d more tickets — %s",
        delivery.acceptance_round,
        len(tickets),
        summary,
    )


def _accepted(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """Push the branch and open (or find) the pull request."""
    flow = make_flow(cfg)
    pre = cfg.preflight
    devops.require_clean(cfg.repo_dir)
    head = devops.head_commit(cfg.repo_dir)
    try:
        devops.push_branch(cfg.repo_dir, store.branch)
        pr = flow.open_pull(store.branch)
        if pr is None:
            title = cfg.deploy_settings.pr_title.format(
                title=issue["title"], number=issue["number"]
            )
            pr = flow.create_pull(title, store.branch, pre.default_branch, pr_body(issue, store))
        if pre.review_source == "request":
            flow.request_review(pr["number"], _reviewer(cfg))
    except (DevopsError, GithubError) as e:
        raise GateFailed(f"could not open the pull request: {e}") from e
    delivery.pr_number = pr["number"]
    delivery.head_sha = head
    delivery.waiting_since = _now()
    store.pr_url = pr.get("html_url", "")
    delivery.stage = "reviewing"
    emit(cfg.events, "pull_request_opened", url=store.pr_url)


def _reviewing(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """Wait until the review passes, raises findings, or the wait times out."""
    settings = cfg.deploy_settings
    flow = make_flow(cfg)
    _sync_push(cfg, store, delivery)
    if delivery.waiting_since is None:
        delivery.waiting_since = _now()
        store.save()
    while True:
        cfg.control.check()
        status = _review_status(flow, cfg, delivery)
        if status.state == "pass":
            _pass(cfg, delivery, "review")
            delivery.waiting_since = None
            delivery.stage = "merging"
            return
        if status.state == "findings":
            delivery.stage = "revising"
            return
        if _now() - delivery.waiting_since >= settings.review_timeout_min * 60:
            if status.findings:
                delivery.stage = "revising"  # act on what is known rather than wait forever
                return
            raise GateFailed(
                f"waited {settings.review_timeout_min} min for {', '.join(status.waiting_for)}"
            )
        log.debug("PR #%s waiting for %s", delivery.pr_number, ", ".join(status.waiting_for))
        _wait(cfg, settings.poll_seconds)


def _revising(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """Hand the findings to the reviser, then push, reply, resolve and re-request."""
    settings = cfg.deploy_settings
    flow = make_flow(cfg)
    _sync_push(cfg, store, delivery)
    findings = _review_status(flow, cfg, delivery).findings
    if not findings:
        delivery.stage = "reviewing"
        return
    if delivery.review_round >= settings.max_review_rounds:
        summary = "; ".join(f"{f.where}: {f.text[:120]}" for f in findings)
        _comment(
            flow,
            delivery,
            f"issue-runner stopped after {delivery.review_round} review round(s). "
            "Open findings:\n\n" + "\n".join(f"- `{f.where}`: {f.text}" for f in findings),
        )
        raise GateFailed(
            f"{len(findings)} finding(s) still open after {delivery.review_round} review "
            f"round(s): {summary}"
        )
    try:
        revision = review.revise(client, cfg, issue, store, delivery.pr_number, findings)
    except (review.ReviewError, CopilotError, DevopsError) as e:
        raise GateFailed(str(e)) from e

    by_ref = {f.ref: f for f in findings}
    replies: dict[str, str] = {}
    for ref, (action, reason) in revision.actions.items():
        finding = by_ref[ref]
        if finding.kind != "thread":
            continue
        if action == "fixed" and revision.changed:
            replies[ref] = f"Fixed: {reason}"
        elif action == "not_applicable":
            agreed, why = review.agrees(client, finding, reason)
            if agreed:
                replies[ref] = f"Not changed: {reason} (a second reviewer agreed: {why})"
            else:
                log.info("declining %s was not agreed: %s", finding.where, why)

    try:
        if revision.changed:
            sha = devops.commit_changes(
                cfg.repo_dir,
                f"fix(review): address review round {delivery.review_round + 1}",
                store.branch,
            )
            delivery.head_sha = store.last_commit = sha
            store.save()
            devops.push_branch(cfg.repo_dir, store.branch)
            replies = {ref: f"{body} ({sha[:12]})" for ref, body in replies.items()}
        for ref, body in replies.items():
            if ref in delivery.handled_threads:
                continue
            flow.reply_to_thread(ref, body)
            flow.resolve_thread(ref)
            delivery.handled_threads.append(ref)
            store.save()
        if revision.changed and not cfg.preflight.review_on_push:
            flow.request_review(delivery.pr_number, _reviewer(cfg))
    except (DevopsError, GithubError) as e:
        raise GateFailed(f"could not publish the review fixes: {e}") from e
    delivery.review_round += 1
    delivery.waiting_since = _now()
    delivery.stage = "reviewing"


def _merging(cfg, client, issue, store: TicketStore, delivery: Delivery) -> None:
    """Merge the reviewed head; bring the base in first if GitHub says it must."""
    settings = cfg.deploy_settings
    pre = cfg.preflight
    flow = make_flow(cfg)
    number = delivery.pr_number
    if delivery.gates["review"] != "pass":
        delivery.stage = "reviewing"
        return
    _sync_push(cfg, store, delivery)
    pr = _settled_pull(cfg, flow, number)
    head = (pr.get("head") or {}).get("sha")
    if pr.get("merged"):
        if head != delivery.head_sha:
            raise GateFailed(f"PR #{number} was merged outside the runner at an unreviewed head")
        _merged(cfg, delivery, pr.get("merge_commit_sha"))
        return
    if pr.get("state") != "open":
        raise GateFailed(f"PR #{number} is {pr.get('state')}")
    if head != delivery.head_sha:
        raise GateFailed(f"PR #{number} head moved to {str(head)[:12]} outside the runner")

    state = pr.get("mergeable_state")
    if state in ("behind", "dirty"):
        _update_branch(cfg, client, issue, store, delivery, flow, state)
        return
    title = f"{pr.get('title')} (#{number})" if pre.merge_method == "squash" else None
    try:
        flow.merge_pull(number, pre.merge_method, delivery.head_sha, title)
    except GithubError as e:
        if "HTTP 409" in str(e):
            raise GateFailed(f"PR #{number} head changed before the merge") from e
        if "HTTP 405" in str(e) and delivery.merge_attempts < settings.max_merge_attempts:
            # not mergeable right now (the base just moved): look again shortly
            delivery.merge_attempts += 1
            log.info("PR #%s is not mergeable yet: %s", number, e)
            _wait(cfg, settings.poll_seconds)
            return
        raise GateFailed(f"could not merge PR #{number}: {e}") from e
    merged = flow.pull(number)
    if not merged.get("merged"):
        raise GateFailed(f"GitHub accepted the merge but PR #{number} is not merged")
    _merged(cfg, delivery, merged.get("merge_commit_sha"))


def _update_branch(cfg, client, issue, store, delivery: Delivery, flow, state: str) -> None:
    settings = cfg.deploy_settings
    if delivery.merge_attempts >= settings.max_merge_attempts:
        raise GateFailed(
            f"PR #{delivery.pr_number} is still {state} after {delivery.merge_attempts} "
            f"update(s) from {cfg.preflight.default_branch}"
        )
    delivery.merge_attempts += 1
    store.save()
    try:
        sha = merge.update_branch(client, cfg, issue, store, cfg.preflight.default_branch)
    except (merge.MergeError, CopilotError, DevopsError) as e:
        raise GateFailed(str(e)) from e
    delivery.head_sha = sha
    store.save()
    try:
        devops.push_branch(cfg.repo_dir, store.branch)
        if not cfg.preflight.review_on_push:
            flow.request_review(delivery.pr_number, _reviewer(cfg))
    except (DevopsError, GithubError) as e:
        raise GateFailed(f"could not publish the updated branch: {e}") from e
    # a new head is new code: it has to pass review again before it may merge
    delivery.gates["review"] = None
    delivery.waiting_since = _now()
    delivery.stage = "reviewing"


def _merged(cfg, delivery: Delivery, merge_sha: str | None) -> None:
    if not merge_sha:
        raise GateFailed(f"PR #{delivery.pr_number} is merged but GitHub reports no merge commit")
    delivery.merge_sha = merge_sha
    _pass(cfg, delivery, "merge")
    delivery.stage = "deploying"


def _settled_pull(cfg, flow: GitHubFlow, number: int) -> dict:
    """The PR once GitHub has computed whether it can merge (mergeable is null until then)."""
    try:
        for _ in range(10):
            pr = flow.pull(number)
            if pr.get("merged") or pr.get("mergeable") is not None:
                return pr
            _wait(cfg, min(cfg.deploy_settings.poll_seconds, 10))
        return pr
    except GithubError as e:
        raise GateFailed(f"could not read PR #{number}: {e}") from e


def _review_status(flow: GitHubFlow, cfg, delivery: Delivery) -> review.ReviewStatus:
    number = delivery.pr_number
    try:
        pr = flow.pull(number)
        if pr.get("merged"):
            raise GateFailed(f"PR #{number} was merged outside the runner before review passed")
        if pr.get("state") != "open":
            raise GateFailed(f"PR #{number} is {pr.get('state')}")
        head = (pr.get("head") or {}).get("sha")
        if head != delivery.head_sha:
            raise GateFailed(f"PR #{number} head moved to {str(head)[:12]} outside the runner")
        return review.evaluate(
            head_sha=head,
            checks=flow.check_runs(head),
            statuses=flow.statuses(head),
            reviews=flow.reviews(number),
            threads=flow.review_threads(number),
            bot=cfg.deploy_settings.review_bot,
            required_approvals=cfg.preflight.required_approvals,
        )
    except GithubError as e:
        # a GitHub hiccup is not a review result: keep waiting until the timeout
        log.warning("could not read PR #%s: %s", number, e)
        return review.ReviewStatus("pending", [f"GitHub ({e})"])


def _sync_push(cfg, store: TicketStore, delivery: Delivery) -> None:
    """Publish a committed head that a stopped run had not pushed yet."""
    if delivery.head_sha and devops.head_commit(cfg.repo_dir) == delivery.head_sha:
        try:
            devops.push_branch(cfg.repo_dir, store.branch)
        except DevopsError as e:
            raise GateFailed(f"could not push {store.branch}: {e}") from e


def _wait(cfg, seconds: float) -> None:
    """Sleep in short steps so a stop request is honoured promptly."""
    end = _now() + seconds
    while _now() < end:
        cfg.control.check()
        _sleep(min(1.0, end - _now()))


def _comment(flow: GitHubFlow, delivery: Delivery, body: str) -> None:
    try:
        flow.comment(delivery.pr_number, body)
    except GithubError as e:
        log.warning("could not comment on PR #%s: %s", delivery.pr_number, e)


def _reviewer(cfg) -> str:
    return cfg.deploy_settings.review_bot.removesuffix("[bot]") + "[bot]"


def pr_body(issue: dict, store: TicketStore) -> str:
    """Refs, not Closes: the issue closes only when the whole Definition of Done is met."""
    lines = [f"Refs #{issue['number']}", ""]
    if store.plan_summary:
        lines += [store.plan_summary, ""]
    criteria = store.delivery.criteria if store.delivery else []
    if criteria:
        lines += [
            "### Acceptance criteria",
            "",
            "| | Criterion | Proven by | Before merge |",
            "|---|---|---|---|",
        ]
        for c in criteria:
            text = c["text"].replace("|", "\\|")
            if c["where"] == "tests":
                tickets = ", ".join(str(t) for t in c["tickets"]) or "none"
                proven, before = f"tests in tickets {tickets}", c.get("pre_merge") or "not judged"
            else:
                proven, before = "a Dev check after deployment", "check fails on Dev today"
            lines.append(f"| {c['id']} | {text} | {proven} | {before} |")
        lines.append("")
    lines.append("### Tickets")
    for ticket in store.tickets:
        if ticket.status == "done":
            lines.append(f"- {ticket.id}. {ticket.title} — asserts `{ticket.test_assertion}`")
    lines += [
        "",
        (
            "Opened by issue-runner `--deploy`. The issue closes only when its Definition of "
            "Done is met: acceptance criteria, code review, merge and the Dev deployment."
        ),
    ]
    return "\n".join(lines)


def _not_implemented(cfg, client, issue, store, delivery) -> None:
    raise GateFailed(f"delivery stage {delivery.stage!r} is not implemented yet")


_HANDLERS = dict.fromkeys(STAGE_GATE, _not_implemented)
_HANDLERS["built"] = _built
_HANDLERS["dev_checks"] = _dev_checks
_HANDLERS["accepted"] = _accepted
_HANDLERS["reviewing"] = _reviewing
_HANDLERS["revising"] = _revising
_HANDLERS["merging"] = _merging
