"""Delivery for --deploy: from built tickets to a verified Dev deployment.

The Definition of Done is six gates (tickets.DOD_GATES). The run succeeds only
when every gate passes; anything else is a failure that names the gate it
stopped at. Each stage saves state before and after its work, so a stopped or
crashed run resumes at the same stage, and a failed stage is retried by
re-running the same command.
"""

import logging

from ..budget import BudgetExhausted
from ..config import RunnerConfig
from ..copilot import CopilotError
from ..events import emit
from ..tickets import Delivery, Ticket, TicketStore
from . import acceptance
from .plan import PlanError, plan_extra

log = logging.getLogger("issue_runner")

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


def _not_implemented(cfg, client, issue, store, delivery) -> None:
    raise GateFailed(f"delivery stage {delivery.stage!r} is not implemented yet")


_HANDLERS = dict.fromkeys(STAGE_GATE, _not_implemented)
_HANDLERS["built"] = _built
_HANDLERS["dev_checks"] = _dev_checks
