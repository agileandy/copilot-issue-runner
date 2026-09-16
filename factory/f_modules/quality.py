"""Deterministic command blocks, and their envelope adapter.

§7.1's known-command rule: *if you can write the invocation down, it belongs in a* ``kind="code"``
*phase*. This is why there is no tester agent — ``pytest`` is a command, not a judgement call, and an
agent rediscovering the test runner every run spends a context window learning what a subprocess
already knows.

A failing block does **not** fail its phase. The runner did its job; the *code* is what failed. That
distinction is what makes §1.4 work: a test phase that ran a red suite succeeded, and the run must
not.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from f_modules.data_types import (
    EnvelopeStatus,
    QualityCheckResult,
    QualityCheckSpec,
    QualityResult,
    VerifyOutput,
)
from f_modules.utils import operator_env

TAIL_BYTES = 4000

PLACEHOLDER_ARGV = ["false"]
"""Marker argv for a block nobody has written yet.

§7.2 rule 3: ship placeholders that **announce they are fake**, not plausible guesses. A
wrong-but-plausible command that silently exits 0 is far worse than one that says so out loud —
it reports a quality bar that was never checked.
"""


def placeholder(name: str, area: str, operation: str) -> QualityCheckSpec:
    """A block that fails loudly until someone writes the real invocation."""
    return QualityCheckSpec(
        name=name,
        area=area,
        operation=operation,
        argv=["sh", "-c", f'echo "PLACEHOLDER: no {operation} command configured for {area}"; exit 1'],
        configured=False,
    )


class NotConfigured(Exception):
    """A chain whose suite is still a placeholder was asked to run it.

    Raised beside roster and repository validation, before anything spawns. The alternative is what
    happens without it: the placeholder fails, the fix loop reads that as a red suite, and a builder
    spends three turns editing code to satisfy a command that does not exist yet.
    """


def require_configured(specs=None) -> None:
    """Refuse if any block a chain is about to run is still a placeholder (§7.2)."""
    unwritten = [s for s in (DEFAULT_CHECKS if specs is None else specs) if not s.configured]
    if unwritten:
        listed = ", ".join(f"{s.area}/{s.operation}" for s in unwritten)
        raise NotConfigured(
            f"no real command configured for: {listed}. This chain runs your suite and commits "
            "only if it is green, so it needs one. Edit DEFAULT_CHECKS in "
            "factory/f_modules/quality.py — e.g. argv=[\"uv\", \"run\", \"pytest\", \"-q\"]."
        )


# Starter blocks for this repo's own toolchain. Binaries are called by **bare name** — never an
# absolute path, which would bake one machine into the trace.
DEFAULT_CHECKS: tuple[QualityCheckSpec, ...] = (
    placeholder("tests", "python", "test"),
)
"""What a **stamped** repo starts with (§7.2 rule 3).

This used to be ``uv run pytest -q`` — this repo's own toolchain, which is a plausible guess and
therefore the wrong kind of default. In a fresh repo it does not fail as a red suite; it fails as
``Failed to spawn: pytest``, which no amount of building can fix.
"""


def run_check(spec: QualityCheckSpec, artifact_dir: Path | None = None,
              cwd: Path | str | None = None) -> QualityCheckResult:
    """Run one block. ``argv`` list, never a shell string — no quoting bugs, no injection."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            spec.argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=spec.timeout_seconds,
            cwd=str(cwd) if cwd else None,
            env=operator_env(),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        returncode = proc.returncode
    except FileNotFoundError as exc:
        output = f"{exc}\n"
        returncode = 127
    except subprocess.TimeoutExpired:
        output = f"timed out after {spec.timeout_seconds}s\n"
        returncode = 124

    duration = time.monotonic() - started

    artifact = ""
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"{spec.name}.log"
        path.write_text(output)
        artifact = str(path)

    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=" ".join(spec.argv),
        returncode=returncode,
        # Judge commands by exit status, not by output text (§11.3).
        passed=returncode == 0,
        duration_seconds=round(duration, 3),
        output_artifact=artifact,
        output_tail=output[-TAIL_BYTES:],
    )


def run_quality(
    specs: tuple[QualityCheckSpec, ...] | list[QualityCheckSpec] = DEFAULT_CHECKS,
    artifact_dir: Path | None = None,
    cwd: Path | str | None = None,
) -> QualityResult:
    """Run **every** block and collect **all** failures — one pass tells you everything (§7.2)."""
    results = [run_check(spec, artifact_dir, cwd) for spec in specs]
    failures = [r for r in results if not r.passed]
    return QualityResult(
        passed=not failures,
        checks=results,
        failures=failures,
        artifacts=[r.output_artifact for r in results if r.output_artifact],
    )


def as_envelope(result: QualityResult, name: str = "quality") -> VerifyOutput:
    """Adapt a code result into the same door an agent's report came through (§5.3).

    The failure evidence rides **inside** the envelope (§7.3): a builder cannot open a log file it
    was never handed. ``output_tail`` is deliberately raw and unparsed, because every runner formats
    failures differently and a generic parser would be confidently wrong.

    Note the status: ``success`` means *this check ran*, not that it was favourable — the same
    distinction §11.3 draws for a reviewer that completes a review and reports ``approved: false``.
    """
    failures = [
        f"{check.name} ({check.command}) exited {check.returncode}\n{check.output_tail}"
        for check in result.failures
    ]
    summary = (
        f"{name}: {len(result.checks) - len(result.failures)}/{len(result.checks)} blocks passed"
    )
    return VerifyOutput(
        status=EnvelopeStatus.SUCCESS,
        summary=summary,
        artifacts=result.artifacts,
        passed=result.passed,
        failures=failures,
    )
