"""Real demo entry point: clone the permanent seed before starting the pipeline."""

from .. import github_io
from ..config import RunnerConfig
from ..github_io import GithubError
from ..phases.devops import require_clean
from ..trackers import resolve

SEED_REPO = "agileandy/copilot-issue-runner"
SEED_NUMBER = 54
SEED_URL = f"https://github.com/{SEED_REPO}/issues/{SEED_NUMBER}"


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
    cfg.github_tickets = False
    cfg.tickets_backend = None
    cfg.open_pr = False
    seed = github_io.fetch_issue(str(SEED_NUMBER), repo=SEED_REPO)
    if dry_run:
        print(f"demo dry run - would clone {SEED_URL} and run only the new issue", flush=True)
        return seed
    clone = github_io.create_issue(SEED_REPO, seed["title"], seed["body"])
    if is_seed(clone, SEED_REPO):
        raise GithubError("GitHub did not return a distinct clone; refusing to run the seed")
    print(
        f"real demo - cloned seed #{SEED_NUMBER} to #{clone['number']}\n"
        f"  {clone['url']}\n"
        "  Real model calls and credits; commits stay local. The seed remains open.\n",
        flush=True,
    )
    return clone
