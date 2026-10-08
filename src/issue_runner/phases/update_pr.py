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
        _prepare_toolchain(wt_cfg, Path(cfg.repo_dir).resolve())
        if client_config is not None:
            client_config.repo_dir = wt
        return update_pr_branch(client, wt_cfg, branch, base)
    finally:
        if client_config is not None:
            client_config.repo_dir = original_client_dir
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
