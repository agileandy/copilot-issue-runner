""""What changed since main" is two git commands and a subtraction, so it is code (§7.5).

The base is **resolved, not assumed**, and the resolution records *why*. A diff is only as
trustworthy as the thing it was taken against, so the reason travels in the trace instead of leaving
the reader to infer it.
"""

from __future__ import annotations

from pathlib import Path

from f_modules import git_helper as git
from f_modules.data_types import (
    ChangeBaseReason,
    ChangeCapture,
    ChangesOutput,
    EnvelopeStatus,
)

TRUNCATION_MARKER = "\n\n[diff truncated at {bytes} bytes — see the full diff with: {command}]\n"


class ChangeSet:
    """A resolved change-set: the base, why that base, and what differs from it."""

    def __init__(
        self,
        base: str,
        reason: ChangeBaseReason,
        changed_files: list[str],
        untracked: list[str],
        insertions: int,
        deletions: int,
        stat: str,
        diff_path: str,
        truncated: bool,
    ) -> None:
        self.base = base
        self.reason = reason
        self.changed_files = changed_files
        self.untracked = untracked
        self.insertions = insertions
        self.deletions = deletions
        self.stat = stat
        self.diff_path = diff_path
        self.truncated = truncated

    @property
    def is_empty(self) -> bool:
        return not self.changed_files


def resolve_base(ref: str, cwd: Path | str | None = None) -> tuple[str, ChangeBaseReason]:
    """Choose the base to diff against, and say why (§7.5).

    ================================  ==============  =====================================
    Situation                         Base            Reason recorded
    ================================  ==============  =====================================
    HEAD ahead of ref                 merge-base      every commit since, plus working tree
    HEAD on ref, tree dirty           HEAD            the uncommitted working tree
    HEAD on ref, tree clean           ``HEAD~1``      falling back to the last commit
    No parent commit                  HEAD            clean tree, no parent
    ================================  ==============  =====================================
    """
    dirty = git.is_dirty(cwd=cwd)

    if git.ref_exists(ref, cwd=cwd):
        base = git.merge_base(ref, "HEAD", cwd=cwd)
        if base != git.head(cwd=cwd):
            # HEAD carries commits the ref does not.
            return base, ChangeBaseReason.AHEAD_OF_REF

    if dirty:
        return git.head(cwd=cwd), ChangeBaseReason.DIRTY_TREE
    if git.has_parent(cwd=cwd):
        return "HEAD~1", ChangeBaseReason.CLEAN_TREE
    return git.head(cwd=cwd), ChangeBaseReason.NO_PARENT


def capture(params: ChangeCapture, cwd: Path | str | None = None) -> ChangeSet:
    """Capture the change-set described by ``params``."""
    base, reason = resolve_base(params.ref, cwd=cwd)

    numstat = git.diff_numstat(base, cwd=cwd)
    changed = [path for _, _, path in numstat]
    insertions = sum(added for added, _, _ in numstat)
    deletions = sum(removed for _, removed, _ in numstat)
    stat = git.diff_stat(base, cwd=cwd)

    untracked: list[str] = []
    if params.include_untracked:
        # Untracked files never appear in `git diff`, so they are named explicitly rather than
        # going silently missing from a change-set that claims to be complete.
        untracked = git.untracked_files(cwd=cwd)
        changed = changed + [path for path in untracked if path not in changed]

    body = git.diff(base, cwd=cwd)
    truncated = len(body.encode()) > params.max_diff_bytes
    if truncated:
        body = body.encode()[: params.max_diff_bytes].decode("utf-8", "ignore")
        body += TRUNCATION_MARKER.format(
            bytes=params.max_diff_bytes, command=f"git diff {base}"
        )

    header = [f"base: {base}", f"reason: {reason.value}"]
    if untracked:
        header.append("untracked files (absent from git diff by construction):")
        header += [f"  {path}" for path in untracked]

    diff_path = params.diff_path
    if diff_path:
        target = Path(diff_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(header) + "\n\n" + body)

    return ChangeSet(
        base=base,
        reason=reason,
        changed_files=changed,
        untracked=untracked,
        insertions=insertions,
        deletions=deletions,
        stat=stat,
        diff_path=diff_path,
        truncated=truncated,
    )


def as_envelope(changes: ChangeSet) -> ChangesOutput:
    """Adapt a code result into the same door an agent's report came through (§5.3)."""
    return ChangesOutput(
        status=EnvelopeStatus.SUCCESS,
        summary=(
            f"{len(changes.changed_files)} files changed against {changes.base} "
            f"({changes.reason.value}): +{changes.insertions} -{changes.deletions}"
        ),
        artifacts=[changes.diff_path] if changes.diff_path else [],
        base=changes.base,
        changed_files=changes.changed_files,
        insertions=changes.insertions,
        deletions=changes.deletions,
        stat=changes.stat,
        diff_path=changes.diff_path,
    )
