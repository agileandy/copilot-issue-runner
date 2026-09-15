"""Real demo entry point: clone the permanent seed before starting the pipeline."""

import json
import subprocess
from pathlib import Path

from .. import github_io
from ..config import RunnerConfig
from ..github_io import GithubError
from ..phases.devops import require_clean
from ..ticket_mirror import GithubTickets
from ..trackers import resolve

SEED_REPO = "agileandy/copilot-issue-runner"
SEED_NUMBER = 54
SEED_URL = f"https://github.com/{SEED_REPO}/issues/{SEED_NUMBER}"
SEED_MARKER = "<!-- issue-runner-permanent-demo-seed:"


class DemoCleanIncomplete(GithubError):
    """Local cleanup succeeded but some issues could not be retired."""


def is_seed(issue: dict, repo: str | None) -> bool:
    return issue["number"] == SEED_NUMBER and (
        (repo or "").casefold() == SEED_REPO or issue.get("url") == SEED_URL
    )


def load_demo_issue(cfg: RunnerConfig, *, dry_run: bool = False) -> dict:
    info = resolve(cfg.repo_dir)
    if (
        info.kind != "github"
        or info.owner_repo.casefold() != SEED_REPO
        or (cfg.repo is not None and cfg.repo.casefold() != SEED_REPO)
    ):
        raise GithubError(f"--demo must run from a checkout of {SEED_REPO}")
    if not dry_run:
        require_clean(cfg.repo_dir)
    cfg.repo = SEED_REPO
    cfg.tickets_backend = None
    cfg.open_pr = False
    seed = github_io.fetch_issue(str(SEED_NUMBER), repo=SEED_REPO)
    if dry_run:
        print(f"demo dry run - would clone {SEED_URL} and run only the new issue", flush=True)
        return seed
    clone = github_io.create_issue(SEED_REPO, seed["title"], seed["body"])
    if is_seed(clone, SEED_REPO):
        raise GithubError("GitHub did not return a distinct clone; refusing to run the seed")
    if cfg.github_tickets:
        cfg.tickets_backend = GithubTickets(SEED_REPO)
    print(
        f"real demo - cloned seed #{SEED_NUMBER} to #{clone['number']}\n"
        f"  {clone['url']}\n"
        "  Real model calls and credits; commits stay local. The seed remains open.\n"
        + (
            f"  Tickets are mirrored as sub-issues of #{clone['number']}; "
            f"remove them with: gh-runner --demo-clean {clone['number']}\n"
            if cfg.github_tickets
            else ""
        ),
        flush=True,
    )
    return clone


def clean_demo_clone(repo_dir, number: int) -> None:
    """Retire one demo clone, its sub-issues, and the local work it generated.

    Deleting an issue needs admin rights, which an ordinary `repo` token does
    not have, so a refused delete falls back to closing. The local cleanup runs
    either way: half-removed GitHub state must not strand a worktree.
    """
    if number == SEED_NUMBER:
        raise GithubError(f"issue #{SEED_NUMBER} is the permanent seed and must never be deleted")
    repo_dir = Path(repo_dir)
    info = resolve(repo_dir)
    if info.kind != "github" or info.owner_repo.casefold() != SEED_REPO:
        raise GithubError(f"--demo-clean must run from a checkout of {SEED_REPO}")
    issue = github_io.fetch_issue(str(number), repo=SEED_REPO)
    if SEED_MARKER not in (issue.get("body") or ""):
        raise GithubError(f"issue #{number} is not a demo clone of #{SEED_NUMBER}; refusing")

    subissues = github_io.list_subissues(SEED_REPO, number)
    deleted, closed, failed = [], [], []
    for target in [*subissues, number]:
        _retire_issue(target, deleted, closed, failed)
    removed = _clean_local_run(repo_dir, number)

    outcome = ", ".join(
        part
        for part in (
            f"deleted {len(deleted)} issue(s)" if deleted else "",
            f"closed {len(closed)} issue(s) (your token cannot delete)" if closed else "",
        )
        if part
    )
    print(
        f"demo clone #{number}: {outcome or 'no issue could be retired'}; "
        f"removed {removed}. The seed #{SEED_NUMBER} is untouched.",
        flush=True,
    )
    if failed:
        raise DemoCleanIncomplete(
            f"local work was removed, but GitHub issues {failed} could not be "
            "deleted or closed; retire them by hand"
        )


def _retire_issue(number: int, deleted: list, closed: list, failed: list) -> None:
    try:
        github_io.delete_issue(SEED_REPO, number)
        deleted.append(number)
        return
    except GithubError:
        pass
    try:
        github_io.close_subissue(SEED_REPO, number, "retired by gh-runner --demo-clean")
        closed.append(number)
    except GithubError:
        failed.append(number)


def _clean_local_run(repo_dir: Path, number: int) -> str:
    """Remove the run worktree, branch and state this demo clone generated.

    Scoped by the issue number: only a worktree at worktrees/<number> and a
    branch named issue-<number>-* are touched, so unrelated work survives.
    """
    state_dir = repo_dir / ".issue-runner"
    state_file = state_dir / f"issue-{number}.json"
    branch = None
    if state_file.is_file():
        try:
            branch = json.loads(state_file.read_text()).get("branch")
        except (OSError, ValueError):
            branch = None
    worktree = state_dir / "worktrees" / str(number)
    if worktree.exists():
        _git(repo_dir, "worktree", "remove", "--force", str(worktree))
    _git(repo_dir, "worktree", "prune")
    if branch and branch.startswith(f"issue-{number}-"):
        _git(repo_dir, "branch", "-D", branch)
    for path in (state_file, state_dir / f"usage-issue-{number}.json"):
        path.unlink(missing_ok=True)
    parts = ["the run worktree", f"branch {branch}" if branch else None, "saved run state"]
    return ", ".join(p for p in parts if p)


def _git(repo_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=False)
