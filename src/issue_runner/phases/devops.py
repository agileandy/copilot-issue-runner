"""Devops phase: branch management and per-ticket commits in the TARGET repo.

Plain git only — the runner is generic and cannot assume any host tooling.
Committing on main/master is refused: the whole run happens on the issue branch.
"""

import re
import subprocess
from pathlib import Path

from ..tickets import Ticket


class DevopsError(RuntimeError):
    pass


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise DevopsError(f"git {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result


def current_branch(repo_dir: Path) -> str:
    return _git(repo_dir, "branch", "--show-current").stdout.strip()


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "change"


# junk a `git add -A` must never sweep into a ticket commit
DEFAULT_EXCLUDES = (".issue-runner/", "__pycache__/", "*.pyc", ".pytest_cache/")


def create_branch(repo_dir: Path, issue_ref: str, slug: str) -> str:
    branch = f"issue-{issue_ref}-{slug}" if slug else f"issue-{issue_ref}"
    exists = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=str(repo_dir),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        _git(repo_dir, "switch", branch)
    else:
        _git(repo_dir, "switch", "-c", branch)
    return branch


def ensure_excluded(repo_dir: Path, pattern: str) -> None:
    """Keep runner state out of the target repo's commits without touching .gitignore."""
    exclude = Path(repo_dir) / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    existing = exclude.read_text() if exclude.exists() else ""
    if pattern not in existing:
        exclude.write_text(existing.rstrip("\n") + f"\n{pattern}\n")


def commit_ticket(repo_dir: Path, ticket: Ticket) -> str:
    branch = current_branch(repo_dir)
    if branch in ("main", "master"):
        raise DevopsError(f"refusing to commit on {branch} — the run must be on an issue branch")
    _git(repo_dir, "add", "-A")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(repo_dir), capture_output=True, check=False
    ).returncode
    if staged == 0:
        raise DevopsError(f"nothing to commit for ticket {ticket.id}")
    message = (
        f"feat(ticket-{ticket.id}): {ticket.title}\n\n"
        f"- assertion: {ticket.test_assertion}\n\n"
        "Co-authored with AI"
    )
    _git(repo_dir, "commit", "-m", message)
    return _git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
