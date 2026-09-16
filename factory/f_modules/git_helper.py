"""Git plumbing, and nothing else.

Every command here is a *known command* (§7.1) — you can write the invocation down, so no agent runs
it. Arguments are always argv lists, never shell strings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from f_modules.utils import operator_env


class GitError(Exception):
    pass


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=operator_env(),
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def is_repo(cwd: Path | str | None = None) -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree", cwd=cwd).strip() == "true"
    except GitError:
        return False


def head(cwd: Path | str | None = None) -> str:
    return git("rev-parse", "HEAD", cwd=cwd).strip()


def has_parent(cwd: Path | str | None = None) -> bool:
    return git("rev-parse", "--verify", "-q", "HEAD~1", cwd=cwd, check=False).strip() != ""


def ref_exists(ref: str, cwd: Path | str | None = None) -> bool:
    return git("rev-parse", "--verify", "-q", ref, cwd=cwd, check=False).strip() != ""


def merge_base(a: str, b: str, cwd: Path | str | None = None) -> str:
    return git("merge-base", a, b, cwd=cwd).strip()


def is_ancestor(maybe_ancestor: str, ref: str, cwd: Path | str | None = None) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, ref],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=operator_env(),
    )
    return proc.returncode == 0


def is_dirty(cwd: Path | str | None = None) -> bool:
    return bool(git("status", "--porcelain", cwd=cwd).strip())


def status_porcelain(cwd: Path | str | None = None) -> list[tuple[str, str]]:
    """``[(xy, path), ...]`` — the raw change-set, which permissions fingerprints (§4.2)."""
    entries: list[tuple[str, str]] = []
    for line in git("status", "--porcelain", "-z", "--untracked-files=all", cwd=cwd).split("\0"):
        if len(line) < 4:
            continue
        entries.append((line[:2], line[3:]))
    return entries


def untracked_files(cwd: Path | str | None = None) -> list[str]:
    """Untracked files are absent from ``git diff`` by construction (§7.5)."""
    out = git("ls-files", "--others", "--exclude-standard", cwd=cwd)
    return [line for line in out.splitlines() if line]


def is_tracked(path: str, cwd: Path | str | None = None) -> bool:
    return git("ls-files", "--error-unmatch", path, cwd=cwd, check=False).strip() != ""


def diff(base: str, cwd: Path | str | None = None) -> str:
    return git("diff", base, cwd=cwd)


def diff_stat(base: str, cwd: Path | str | None = None) -> str:
    return git("diff", "--stat", base, cwd=cwd)


def diff_numstat(base: str, cwd: Path | str | None = None) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for line in git("diff", "--numstat", base, cwd=cwd).splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        rows.append((_count(added), _count(removed), path))
    return rows


def _count(value: str) -> int:
    return 0 if value == "-" else int(value or 0)


def checkout_path(path: str, cwd: Path | str | None = None) -> None:
    """Restore one tracked path from HEAD. Used only by permissions rollback (§4.4)."""
    git("checkout", "--", path, cwd=cwd)


class NothingToCommit(GitError):
    """A commit phase found an empty index.

    Raised rather than skipped. An agent that reported success while changing nothing is a real
    problem, and a commit phase that quietly does nothing turns it into a run that looks clean —
    the trace would show a commit phase succeeding with no commit behind it.
    """


class NotAWorkingTree(GitError):
    """The factory was pointed at something that is not a git repository.

    Two things refuse on this single condition, and they are deliberately the same class rather
    than two that could drift apart:

    - **Enforcement** (§4.2) compares git change-sets, so without a repository there is no boundary
      to enforce and an agent would run unattributed. That is a refusal, not a degraded mode —
      silently skipping enforcement is the one failure permissions must never have.
    - **:func:`require_repo`**, which asks the question before anything spawns, because a chain
      whose commit phase is last would otherwise pay for every agent in it first.
    """


def require_repo(cwd: Path | str | None = None) -> None:
    """Refuse, before anything spawns, if a committing chain has no repository to commit into.

    §3.2's economics applied to the one precondition that is not about the roster: validation is
    free, spawning is not. One chain reached its commit phase after 1.27M tokens and could not
    commit a line of it.
    """
    if not is_repo(cwd=cwd):
        raise NotAWorkingTree(
            "not a git repository: this workflow ends in a commit phase, which needs one. "
            "Run `git init` in the repo root (and make a first commit) before running it."
        )


def commit_all(message: str, cwd: Path | str | None = None) -> str:
    git("add", "-A", cwd=cwd)
    if not git("status", "--porcelain", cwd=cwd).strip():
        raise NothingToCommit(
            "nothing to commit: the working tree is clean, so whoever reported success "
            "changed no files"
        )
    git("commit", "-m", message, cwd=cwd)
    return head(cwd=cwd)


def commit_message_for(envelope, fallback: str) -> str:
    """The message for a commit phase, with the fallback §5.4 requires.

    ``commit_message`` belongs to its author and describes **its own** work product, so a chain that
    commits per step never reuses one agent's words for another agent's diff. It defaults empty,
    which is why every commit phase needs a fallback rather than committing with a blank message.
    """
    return (getattr(envelope, "commit_message", "") or "").strip() or fallback
