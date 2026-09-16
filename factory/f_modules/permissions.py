"""Snapshot, enforce, roll back.

§4.1's core insight, which a tool allowlist cannot deliver:

    ``tools:`` is a capability list. ``writes:`` is the boundary.

``bash`` runs *anything*, including ``git checkout factory/`` — not hypothetical: a builder given
bash to run tests used it to discard uncommitted changes to the very quality check about to judge it.
And ``write`` reaches any path, not just the one report file it was granted for.

Enforcement is after-the-fact, like everything else in this system (§4.2)::

    1. snapshot()  — fingerprint the change-set BEFORE the agent's first prompt
    2. ...agent runs: first prompt, JSON repairs, gate corrections — all against that baseline
    3. enforce()   — fingerprint again; diff the change-sets; attribute every difference
    4. roll back   — undo anything unauthorized the agent INTRODUCED
    5. raise       — the phase dies, naming every offending path and its rollback outcome

**Fingerprint, don't watch.** Comparing change-sets rather than intercepting writes is what catches
the ``git checkout`` case: a path that was dirty before and is clean after has been **reverted**, and
a reversion is a modification. Appearing, disappearing and changing all count.

The value objects here are deliberately local rather than in ``data_types``: a breach is never
persisted as a table of its own — it becomes a phase error and an event payload — so it is an
internal shape, not part of the storage contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from f_modules import git_helper as git
from f_modules.data_types import Defaults, ResolvedAgent

DELETED = "<deleted>"


class PermissionBreach(Exception):
    """An agent wrote outside its boundary.

    §4.5: this is **not** a gate violation. Gates are for work an agent can be asked to redo; a
    breach cannot be corrected by re-prompting, because the write already happened. It aborts the
    phase.
    """

    def __init__(self, report: EnforcementReport) -> None:
        super().__init__(report.summary())
        self.report = report


# ---------------------------------------------------------------------------------------------
# Pattern matching (§4.3)
# ---------------------------------------------------------------------------------------------


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a write/protection pattern.

    Syntax: trailing ``/`` is a directory prefix; ``*`` globs **within one path segment**; ``**``
    crosses segments; anything else is an exact path.

    ``*`` must not cross ``/``. A naive ``fnmatch`` would let ``factory/f_*.py`` match
    ``factory/f_data/sessions/x/y.py``, silently widening every protection pattern into a
    whole-subtree one — the failure would be invisible until an agent wrote somewhere it should not
    have been able to reach.
    """
    if pattern.endswith("/"):
        return re.compile(re.escape(pattern) + ".*")

    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    return bool(_to_regex(pattern).match(path))


def matches_any(patterns: list[str], path: str) -> bool:
    return any(matches(pattern, path) for pattern in patterns)


def is_permitted(path: str, agent: ResolvedAgent, defaults: Defaults) -> bool:
    """§4.3's resolution order, in this order and no other.

    1. Under ``data_dir``      -> ALWAYS permitted (session runtime)
    2. Matched by ``writes``   -> permitted (naming a path unlocks even a protected one)
    3. In ``protected_files``  -> DENIED
    4. Otherwise               -> permitted iff ``writes`` is None
    """
    data_dir = defaults.data_dir.rstrip("/") + "/"
    if path == defaults.data_dir.rstrip("/") or path.startswith(data_dir):
        return True
    if agent.writes is not None and matches_any(agent.writes, path):
        return True
    if matches_any(defaults.protected_files, path):
        return False
    return agent.writes is None


# ---------------------------------------------------------------------------------------------
# Fingerprinting (§4.2)
# ---------------------------------------------------------------------------------------------


def _digest(root: Path, path: str) -> str:
    target = root / path
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    except (OSError, IsADirectoryError):
        return DELETED


NotAWorkingTree = git.NotAWorkingTree
"""Re-exported, not redefined. The preflight (:func:`git_helper.require_repo`) and enforcement
refuse on the same condition, so they raise the same class — two would let a caller catch one and
be surprised by the other."""


def snapshot(cwd: Path | str) -> dict[str, str]:
    """Fingerprint the working tree's change-set.

    Only paths git already considers changed are hashed — the set is small, and an unchanged file
    cannot be the subject of an attribution.
    """
    root = Path(cwd)
    if not git.is_repo(cwd=root):
        raise NotAWorkingTree(
            f"{root} is not a git repository, so write permissions cannot be enforced. "
            "The factory runs on the operator's current branch and needs one."
        )
    return {path: _digest(root, path) for _, path in git.status_porcelain(cwd=root)}


@dataclass
class FileChange:
    path: str
    kind: str
    """``introduced`` | ``modified`` | ``reverted``."""

    permitted: bool = False
    rollback: str = ""


@dataclass
class EnforcementReport:
    changes: list[FileChange] = field(default_factory=list)
    unrecoverable: list[str] = field(default_factory=list)

    @property
    def offenders(self) -> list[FileChange]:
        return [c for c in self.changes if not c.permitted]

    @property
    def clean(self) -> bool:
        return not self.offenders

    def summary(self) -> str:
        lines = [
            f"{c.path}: {c.kind} outside this agent's write boundary ({c.rollback})"
            for c in self.offenders
        ]
        if self.unrecoverable:
            lines.append(
                "unrecoverable — the agent reverted the operator's uncommitted work: "
                + ", ".join(self.unrecoverable)
            )
        return "; ".join(lines)


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[FileChange]:
    """Attribute every difference between two change-sets to the agent."""
    changes: list[FileChange] = []
    for path, digest in after.items():
        if path not in before:
            changes.append(FileChange(path=path, kind="introduced"))
        elif before[path] != digest:
            changes.append(FileChange(path=path, kind="modified"))
    for path in before:
        if path not in after:
            # Dirty before, clean after: the agent reverted the operator's uncommitted work.
            # A reversion is a modification — this is the `git checkout` case.
            changes.append(FileChange(path=path, kind="reverted"))
    return sorted(changes, key=lambda c: c.path)


# ---------------------------------------------------------------------------------------------
# Enforcement and rollback (§4.4)
# ---------------------------------------------------------------------------------------------


def enforce(
    before: dict[str, str],
    agent: ResolvedAgent,
    defaults: Defaults,
    cwd: Path | str,
) -> EnforcementReport:
    """Fingerprint again, attribute, roll back what was introduced, and report.

    Rollback follows §4.4 exactly:

    ==========================================  ==========================================
    Situation                                   Action
    ==========================================  ==========================================
    Clean before, tracked                       ``git checkout -- <path>``
    Clean before, untracked                     delete the file
    **Already dirty before**                    **leave it alone** — report, never discard
    Dirty before and clean after                report as unrecoverable
    ==========================================  ==========================================

    The third row is the one that matters most. Discarding an engineer's uncommitted work to tidy up
    would be the exact harm this module exists to prevent, committed by the cleanup instead of by
    the agent.
    """
    root = Path(cwd)
    after = snapshot(root)
    report = EnforcementReport(changes=diff_snapshots(before, after))

    for change in report.changes:
        change.permitted = is_permitted(change.path, agent, defaults)
        if change.permitted:
            change.rollback = "permitted"
            continue

        if change.kind == "reverted":
            # The write already happened and the content is gone; there is nothing to restore.
            change.rollback = "unrecoverable: the operator's uncommitted work was reverted"
            report.unrecoverable.append(change.path)
            continue

        if change.kind == "modified":
            # It was already dirty before this agent ran, so some of that work is the operator's.
            change.rollback = "left in place: the file was already modified before the agent ran"
            continue

        try:
            if git.is_tracked(change.path, cwd=root):
                git.checkout_path(change.path, cwd=root)
                change.rollback = "reverted with git checkout"
            else:
                (root / change.path).unlink(missing_ok=True)
                change.rollback = "deleted"
        except (OSError, git.GitError) as exc:
            change.rollback = f"rollback failed: {exc}"
            report.unrecoverable.append(change.path)

    return report


def enforce_or_raise(
    before: dict[str, str],
    agent: ResolvedAgent,
    defaults: Defaults,
    cwd: Path | str,
) -> EnforcementReport:
    """As :func:`enforce`, raising :class:`PermissionBreach` when the boundary was crossed."""
    report = enforce(before, agent, defaults, cwd)
    if not report.clean:
        raise PermissionBreach(report)
    return report
