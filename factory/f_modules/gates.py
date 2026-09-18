"""Reusable claim verification.

A gate is a pure function, ``gate(envelope, run) -> GateReport``, and the rules are narrow on
purpose (§6.3):

- **Verify claims, not guesses.** Gate what the envelope *asserted* — its declared artifacts, its
  claimed changed files — never a filename or a count predicted in advance. A gate that knows what
  the answer should be is testing the harness's imagination, not the agent's work.
- **Never early-return.** Inspect every item. Stopping at the first failure hides the rest and turns
  one correction round into three, and each round costs a model call.
- **Never gate taste.** Plan quality, style and architecture are a reviewer's job. Gates check what
  is mechanically checkable.
- **Never hardcode counts.** Use quantity properties, not ``len(...) == 3``.
- **Record checks; do not raise.** The harness decides what a failure means.

And the reason every check is recorded, passing ones included (§6.2): a green gate then *says what
it verified*. ``"plan.md: exists, 2.1KB"`` is evidence; ``passed: true`` is a rumour. On a failed
check the note doubles as the correction sent back to the agent, so evidence and correction are the
same text — written once, read by both a human and a model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from f_modules.data_types import (
    ChangesOutput,
    EnvelopeBase,
    GateReport,
    ReviewOutput,
    VerifyOutput,
)

Gate = Callable[[EnvelopeBase, Any], GateReport]


def _size(path: Path) -> str:
    size = path.stat().st_size
    return f"{size / 1024:.1f}KB" if size >= 1024 else f"{size}B"


def _claimed_artifacts(envelope: EnvelopeBase) -> list[str]:
    return list(getattr(envelope, "artifacts", []) or [])


def artifacts_exist(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """Every declared artifact path exists. The note carries its size as evidence."""
    report = GateReport(gate="artifacts_exist")
    for claim in _claimed_artifacts(envelope):
        path = Path(claim)
        if path.exists():
            report.check(claim, True, f"exists, {_size(path)}")
        else:
            report.check(claim, False, "declared as an artifact but not found on disk")
    return report


def files_non_empty(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """Declared artifacts are not zero bytes."""
    report = GateReport(gate="files_non_empty")
    for claim in _claimed_artifacts(envelope):
        path = Path(claim)
        if not path.exists():
            report.check(claim, False, "declared as an artifact but not found on disk")
        elif path.stat().st_size == 0:
            report.check(claim, False, "exists but is empty")
        else:
            report.check(claim, True, f"{_size(path)}")
    return report


def json_parses(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """Declared ``.json`` artifacts parse."""
    report = GateReport(gate="json_parses")
    for claim in _claimed_artifacts(envelope):
        if not claim.endswith(".json"):
            continue
        path = Path(claim)
        if not path.exists():
            report.check(claim, False, "declared as an artifact but not found on disk")
            continue
        try:
            json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report.check(claim, False, f"does not parse as JSON: {exc}")
        else:
            report.check(claim, True, "parses as JSON")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """Every file claimed changed exists on disk.

    Gates the envelope's own ``changed_files``, never a list the harness expected — an agent that
    reports a file it never wrote is making a claim the harness can refute cheaply.
    """
    report = GateReport(gate="diff_matches_claims")
    for claim in list(getattr(envelope, "changed_files", []) or []):
        path = Path(claim)
        if path.exists():
            report.check(claim, True, f"exists, {_size(path)}")
        else:
            report.check(claim, False, "claimed as changed but not found on disk")
    return report


def verdict_consistent(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """A review's verdict agrees with its own findings.

    The archetype of a **self-refuting-claim** gate: it judges no code at all. An approval that ships
    blocking items, or a rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport(gate="verdict_consistent")
    if not isinstance(envelope, ReviewOutput):
        report.check("verdict", True, "not a review; nothing to refute")
        return report

    unmet = [f.requirement for f in envelope.findings if not f.met]

    if envelope.approved and envelope.blocking:
        report.check(
            "approved",
            False,
            f"approved, but {len(envelope.blocking)} blocking items are listed: "
            f"{'; '.join(envelope.blocking)}",
        )
    elif envelope.approved and unmet:
        report.check(
            "approved",
            False,
            f"approved, but these requirements are reported unmet: {'; '.join(unmet)}",
        )
    elif not envelope.approved and not envelope.blocking and not unmet:
        report.check(
            "approved",
            False,
            "not approved, but no blocking item and no unmet requirement is named",
        )
    else:
        verdict = "approved" if envelope.approved else "rejected"
        report.check("approved", True, f"{verdict}, consistent with its own findings")

    for finding in envelope.findings:
        report.check(
            finding.requirement,
            bool(finding.evidence.strip()) or not finding.met,
            "evidence recorded" if finding.evidence.strip() else "met, but no evidence given",
        )
    return report


def tests_pass(command: list[str], name: str = "tests_pass") -> Gate:
    """Gate **factory**: the given command exits 0; failure carries the output tail."""

    def gate(envelope: EnvelopeBase, run: Any = None) -> GateReport:
        from f_modules.quality import run_check
        from f_modules.data_types import QualityCheckSpec

        report = GateReport(gate=name)
        result = run_check(
            QualityCheckSpec(name=name, area="gate", operation="test", argv=command)
        )
        report.check(
            " ".join(command),
            result.passed,
            f"exited {result.returncode}"
            + ("" if result.passed else f"\n{result.output_tail}"),
        )
        return report

    return gate


def changes_not_empty(envelope: EnvelopeBase, run: Any = None) -> GateReport:
    """A change capture that found nothing is a structural problem, not a quiet success (§13.1)."""
    report = GateReport(gate="changes_not_empty")
    if not isinstance(envelope, (ChangesOutput, VerifyOutput)):
        report.check("changed_files", True, "not a change capture; nothing to check")
        return report
    files = list(getattr(envelope, "changed_files", []) or [])
    report.check(
        "changed_files",
        bool(files),
        f"{len(files)} files changed" if files else "the diff is empty; there is nothing to document",
    )
    return report
