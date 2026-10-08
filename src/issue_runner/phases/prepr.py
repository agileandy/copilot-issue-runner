"""Pre-PR phase: run the configured check commands on the branch and collect findings."""

import shlex
import subprocess

from ..config import RunnerConfig
from ..testreport import strip_ansi
from ..tickets import Ticket, TicketStore
from . import devops

MAX_FINDING_CHARS = 3000


def run_checks(cfg: RunnerConfig, base: str) -> list[dict]:
    """Run each `[pre_pr]` command; return one finding per failing command, in order."""
    findings: list[dict] = []
    guarded = (cfg.repo_dir / ".git").exists()
    for cmd in cfg.pre_pr.commands:
        cfg.control.check()
        cmd = cmd.replace("{base}", base)
        try:
            result = subprocess.run(
                shlex.split(cmd),
                cwd=cfg.repo_dir,
                capture_output=True,
                text=True,
                timeout=cfg.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            findings.append({"command": cmd, "text": str(e)})
            result = None
        if guarded:
            changed = devops.changed_paths(cfg.repo_dir)
            if changed:
                raise devops.DevopsError(
                    f"pre-PR command {cmd} changed the run worktree: {', '.join(changed)}"
                )
        if result is None or result.returncode == 0:
            continue
        text = strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
        findings.append({"command": cmd, "text": text[-MAX_FINDING_CHARS:]})
    return findings


def remaining(store: TicketStore) -> list[dict]:
    """The findings of the last pre-PR round, or [] when no round has run."""
    if not store.pre_pr_rounds:
        return []
    return store.pre_pr_rounds[-1].get("findings", [])


def step(cfg: RunnerConfig, store: TicketStore, report=None) -> bool:
    """Run the pre-PR checks once; turn each finding into a fix ticket. True if any were added."""
    if not cfg.pre_pr.commands or store.pr_url:
        return False
    if store.delivery is not None and store.delivery.pr_number:
        return False
    findings = run_checks(cfg, store.initial_head or "")
    round_number = len(store.pre_pr_rounds) + 1
    store.pre_pr_rounds.append({"round": round_number, "findings": findings})
    if report is not None:
        report.details.append(f"pre-PR round {round_number}: {len(findings)} finding(s)")
        for finding in findings:
            lines = finding["text"].splitlines()
            first = (lines[0] if lines else "").strip()
            report.details.append(f"  {finding['command']}: {first}")
    if round_number > cfg.pre_pr.max_rounds:
        store.save()
        return False
    for finding in findings:
        next_id = max((t.id for t in store.tickets), default=0) + 1
        lines = finding["text"].splitlines()
        first = (lines[0] if lines else "").strip()[:80]
        store.tickets.append(
            Ticket(
                id=next_id,
                title=f"Fix pre-PR finding: {first}",
                description=(
                    f"The pre-PR check `{finding['command']}` failed with:\n\n{finding['text']}"
                ),
                test_assertion=(
                    f"the pre-PR check '{finding['command']}' no longer reports this finding"
                ),
            )
        )
    store.save()
    return bool(findings)
