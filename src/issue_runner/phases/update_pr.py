"""Update an open pull request: merge its base into its head branch in a
worktree of its own, so the caller's checkout is never touched."""

import dataclasses
import subprocess
from pathlib import Path

from ..config import RunnerConfig
from ..orchestrator import _prepare_toolchain
from . import devops
from .merge import MergeError, update_pr_branch


def update_pull_request(cfg: RunnerConfig, client, flow, number: int, state_dir: Path) -> str:
    pr = flow.pull(number)
    if pr.get("state") != "open" or pr.get("merged"):
        state = "merged" if pr.get("merged") else pr.get("state")
        raise MergeError(f"PR #{number} is {state}")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if not head_repo or head_repo.lower() != (base_repo or "").lower():
        raise MergeError(
            f"PR #{number} comes from {head_repo or 'a deleted repository'}, not {base_repo}; "
            "only same-repository PRs can be updated"
        )
    branch = pr["head"]["ref"]
    base = pr["base"]["ref"]
    wt = devops.checkout_branch_worktree(
        cfg.repo_dir, branch, Path(state_dir) / "worktrees" / f"pr-{number}"
    )
    client_config = getattr(client, "config", None)
    original_client_dir = client_config.repo_dir if client_config is not None else None
    wt_cfg = dataclasses.replace(cfg, repo_dir=wt)
    try:
        head = devops.head_commit(wt)
        _prepare_toolchain(wt_cfg, Path(cfg.repo_dir).resolve())
        if client_config is not None:
            client_config.repo_dir = wt
        try:
            return update_pr_branch(client, wt_cfg, branch, base)
        except BaseException:
            _discard_changes(wt, branch, head)
            raise
    finally:
        if client_config is not None:
            client_config.repo_dir = original_client_dir
        _remove_if_clean(cfg.repo_dir, wt)


def _discard_changes(wt: Path, branch: str, head: str) -> None:
    """Return this run's own worktree to `head` after a failed update.

    `git merge --abort` keeps a rejected resolver's edits to files the merge did
    not touch and every file it added, and a dirty worktree is never removed, so
    the next update would refuse to reuse it. A git call that fails or times out
    is ignored, so the update's own error is the one reported.
    """
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--symbolic-full-name", "HEAD"],
            cwd=wt,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        lines = probe.stdout.splitlines()
        # a broken worktree resolves to the caller's checkout, and a switched branch
        # is not this run's to reset
        if (
            probe.returncode != 0
            or len(lines) != 2
            or Path(lines[0]).resolve() != Path(wt).resolve()
            or lines[1] != f"refs/heads/{branch}"
        ):
            return
        for args in (["reset", "--hard", head], ["clean", "-fd"]):
            subprocess.run(["git", *args], cwd=wt, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return


def _remove_if_clean(repo_dir: Path, wt: Path) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if status.returncode == 0 and not status.stdout.strip():
        subprocess.run(
            ["git", "worktree", "remove", str(wt)],
            cwd=repo_dir,
            capture_output=True,
            check=False,
            timeout=60,
        )
