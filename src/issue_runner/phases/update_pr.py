"""Update an open pull request: merge its base into its head branch in a
worktree of its own, so the caller's checkout is never touched."""

import dataclasses
import subprocess
from pathlib import Path

from ..config import RunnerConfig
from . import devops
from .merge import MergeError, update_pr_branch


def update_pull_request(cfg: RunnerConfig, client, flow, number: int, state_dir: Path) -> str:
    pr = flow.pull(number)
    if pr.get("state") != "open" or pr.get("merged"):
        state = "merged" if pr.get("merged") else pr.get("state")
        raise MergeError(f"PR #{number} is {state}")
    branch = pr["head"]["ref"]
    base = pr["base"]["ref"]
    wt = devops.checkout_branch_worktree(
        cfg.repo_dir, branch, Path(state_dir) / "worktrees" / f"pr-{number}"
    )
    try:
        return update_pr_branch(client, dataclasses.replace(cfg, repo_dir=wt), branch, base)
    finally:
        _remove_if_clean(cfg.repo_dir, wt)


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
