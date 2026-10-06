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
from ..events import emit
from ..tickets import Delivery, TicketStore

log = logging.getLogger("issue_runner")

# exit code per failed gate; tickets keeps the existing 1/3/4 codes
GATE_EXIT = {"criteria_tests": 5, "review": 6, "merge": 7, "deploy": 8, "criteria_dev": 5}

# the gate each stage decides; a stage that fails records this gate
STAGE_GATE = {
    "built": "criteria_tests",
    "accepted": "review",
    "reviewing": "review",
    "revising": "review",
    "merging": "merge",
    "deploying": "deploy",
    "verifying": "criteria_dev",
}


class GateFailed(Exception):
    """A delivery stage could not pass its gate. The message is the evidence."""


def run(cfg: RunnerConfig, client, issue: dict, store: TicketStore, report) -> None:
    """Advance the delivery until it is done or a gate fails."""
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
            return
        store.save()
    report.dod_met = True
    report.details.append("definition of done met: every gate passed")


def _not_implemented(cfg, client, issue, store, delivery) -> None:
    raise GateFailed(f"delivery stage {delivery.stage!r} is not implemented yet")


_HANDLERS = dict.fromkeys(STAGE_GATE, _not_implemented)
