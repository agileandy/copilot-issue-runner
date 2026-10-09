"""Pre-PR phase: run the configured check commands on the branch and collect findings."""

import shlex
import subprocess

from ..agent_rules import workspace_rules
from ..config import RunnerConfig
from ..journal import Journal
from ..testreport import strip_ansi
from ..tickets import Ticket, TicketStore
from . import devops
from .build import CoderFailure

MAX_FINDING_CHARS = 3000
TOOL_ERROR_EXIT = 2
MAX_TOOL_ERROR_CHARS = 500
MAX_FINDING_LINES = 20


def finding_lines(finding: dict, limit: int = MAX_FINDING_LINES) -> list[str]:
    """The non-empty stripped lines of a finding, bounded to `limit` plus a truncation note."""
    lines = [line.strip() for line in finding["text"].splitlines() if line.strip()]
    if len(lines) <= limit:
        return lines
    hidden = len(lines) - limit
    return lines[:limit] + [f"… {hidden} more line(s)"]


def _run(cfg: RunnerConfig, cmd: str) -> subprocess.CompletedProcess:
    """Run one already-substituted pre-PR command in the run worktree."""
    try:
        return subprocess.run(
            shlex.split(cmd),
            cwd=cfg.repo_dir,
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise devops.DevopsError(f"pre-PR command {cmd} could not run: {e}") from e


def finding_for(cfg: RunnerConfig, command: str) -> str | None:
    """Re-run one pre-PR command in a dirty worktree; its finding text, or None when it passes."""
    guarded = (cfg.repo_dir / ".git").exists()
    if guarded:
        digest_before = devops.workspace_digest(cfg.repo_dir)
    result = _run(cfg, command)
    if guarded and devops.workspace_digest(cfg.repo_dir) != digest_before:
        raise devops.DevopsError(f"pre-PR command {command} changed the run worktree")
    if result.returncode == 0:
        return None
    text = strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == TOOL_ERROR_EXIT:
        raise devops.DevopsError(
            f"pre-PR command {command} failed with a tool error "
            f"(exit {TOOL_ERROR_EXIT}): {text[:MAX_TOOL_ERROR_CHARS]}"
        )
    return text[-MAX_FINDING_CHARS:]


def run_checks(cfg: RunnerConfig, base: str) -> list[dict]:
    """Run each `[pre_pr]` command; return one finding per failing command, in order."""
    findings: list[dict] = []
    guarded = (cfg.repo_dir / ".git").exists()
    for cmd in cfg.pre_pr.commands:
        cfg.control.check()
        cmd = cmd.replace("{base}", base)
        if guarded:
            head_before = devops.head_commit(cfg.repo_dir)
            branch_before = devops.current_branch(cfg.repo_dir)
        result = _run(cfg, cmd)
        if guarded:
            changed = devops.changed_paths(cfg.repo_dir)
            if changed:
                raise devops.DevopsError(
                    f"pre-PR command {cmd} changed the run worktree: {', '.join(changed)}"
                )
            if (
                devops.head_commit(cfg.repo_dir) != head_before
                or devops.current_branch(cfg.repo_dir) != branch_before
            ):
                raise devops.DevopsError(
                    f"pre-PR command {cmd} moved the run branch; refusing publication"
                )
        if result.returncode == 0:
            continue
        text = strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == TOOL_ERROR_EXIT:
            raise devops.DevopsError(
                f"pre-PR command {cmd} failed with a tool error "
                f"(exit {TOOL_ERROR_EXIT}): {text[:MAX_TOOL_ERROR_CHARS]}"
            )
        findings.append({"command": cmd, "text": text[-MAX_FINDING_CHARS:]})
    return findings


FIX_PROMPT = """\
You are builder.coder fixing a pre-PR check finding on this repository.

Sub-task: {title}
Description: {description}
The check that reports the finding: {command}

Fix the reported finding in the production code. Do NOT suppress, skip, disable
or reconfigure the check, and do not add ignore comments to silence it.
The check is re-run after your change and must pass.

{rules}
When done, reply with ONLY this JSON (no prose):
{{"changed_files": ["<paths you changed>"], "notes": "<one line>"}}
{feedback}"""


def fix_step(
    client,
    cfg: RunnerConfig,
    ticket: Ticket,
    feedback: str | None = None,
    journal: Journal | None = None,
) -> None:
    """Loop builder.coder until the ticket's pre-PR command no longer reports a finding."""
    journal = journal or Journal()
    extra = journal.render(ticket, feedback) if feedback else ""
    last_error = "no attempt made"
    for _ in range(cfg.coder_retries + 1):
        prompt = FIX_PROMPT.format(
            title=ticket.title,
            description=ticket.description,
            command=ticket.pre_pr_command,
            rules=workspace_rules(cfg, ticket=True),
            feedback=extra,
        )
        client.run(prompt, role="builder.coder", session_name=f"coder-t{ticket.id}")
        finding = finding_for(cfg, ticket.pre_pr_command)
        if finding is None:
            return
        last_error = f"the pre-PR check still reports a finding. Output:\n{finding}"
        extra = journal.hand_back(ticket, "harness", "builder.coder", last_error, "change rejected")
    raise CoderFailure(f"builder.coder failed for ticket {ticket.id}: {last_error}")


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
            lines = finding_lines(finding)
            report.details.append(f"  {finding['command']}: {lines[0] if lines else ''}")
            report.details.extend(f"    {line}" for line in lines[1:])
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
                kind="pre_pr",
                phase="coder",
                pre_pr_command=finding["command"],
            )
        )
    store.save()
    return bool(findings)
