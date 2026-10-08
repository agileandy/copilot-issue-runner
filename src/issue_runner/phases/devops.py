"""Devops phase: branch management and per-ticket commits in the TARGET repo.

Plain git only — the runner is generic and cannot assume any host tooling.
Committing on main/master is refused: the whole run happens on the issue branch.
"""

import hashlib
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..testcmd import related_tests
from ..tickets import Ticket


class DevopsError(RuntimeError):
    pass


def _git(repo_dir: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", **(env or {})),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DevopsError(f"could not run git {args[0]} in {repo_dir}: {e}") from e
    if result.returncode != 0:
        raise DevopsError(f"git {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result


def current_branch(repo_dir: Path) -> str:
    return _git(repo_dir, "branch", "--show-current").stdout.strip()


def head_commit(repo_dir: Path) -> str:
    return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()


def head_tree(repo_dir: Path) -> str:
    return _git(repo_dir, "rev-parse", "HEAD^{tree}").stdout.strip()


def changed_paths(repo_dir: Path) -> list[str]:
    paths = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(p for p in _git(repo_dir, *args).stdout.split("\0") if p)
    return sorted(paths)


def diff_since(repo_dir: Path, base: str, max_chars: int = 60000) -> str:
    """The committed change since `base`: a file summary, then as much patch as fits."""
    stat = _git(repo_dir, "diff", "--stat", f"{base}..HEAD").stdout
    patch = _git(repo_dir, "diff", "--no-color", f"{base}..HEAD").stdout
    if len(patch) > max_chars:
        patch = patch[:max_chars] + f"\n... (diff truncated at {max_chars} characters)\n"
    return f"{stat}\n{patch}"


def discard_paths(repo_dir: Path, paths: list[str]) -> None:
    """Restore tracked paths to HEAD and delete untracked ones; nothing else is touched."""
    _validate_paths(repo_dir, paths)
    for name in paths:
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{name}"],
            cwd=str(repo_dir),
            capture_output=True,
            check=False,
        )
        if tracked.returncode == 0:
            _git(repo_dir, "restore", "--source=HEAD", "--staged", "--worktree", "--", name)
        else:
            _git(repo_dir, "rm", "-q", "--cached", "--ignore-unmatch", "--", name)
            (Path(repo_dir) / name).unlink(missing_ok=True)


def require_clean(repo_dir: Path) -> None:
    dirty = _git(repo_dir, "status", "--porcelain", "--untracked-files=all").stdout.strip()
    if dirty:
        raise DevopsError(
            f"refusing a dirty workspace at {repo_dir}; commit or move your changes first:\n"
            f"{dirty[:1000]}"
        )


def worktree_state(repo_dir: Path) -> dict:
    status = _git(repo_dir, "status", "--porcelain", "--untracked-files=all").stdout
    uncommitted = sorted(line[3:] for line in status.splitlines() if line.strip())
    return {
        "path": str(repo_dir),
        "branch": current_branch(repo_dir),
        "head": head_commit(repo_dir),
        "dirty": bool(uncommitted),
        "uncommitted": uncommitted,
    }


def git_common_dir(repo_dir: Path) -> Path:
    raw = _git(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(raw).resolve()


@contextmanager
def _file_lock(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
    except OSError as e:
        raise DevopsError(f"could not open runner lock {path}: {e}") from e
    with stream:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise DevopsError(f"another issue-runner owns this repository ({path})") from e
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def repository_lock(repo_dir: Path):
    return _file_lock(git_common_dir(repo_dir) / "issue-runner.lock")


def state_lock(state_dir: Path):
    return _file_lock(state_dir / "run.lock")


def workspace_digest(repo_dir: Path, *, include_index: bool = True) -> str:
    digest = hashlib.sha256()
    values = [head_commit(repo_dir), current_branch(repo_dir)]
    if include_index:
        values.append(_git(repo_dir, "write-tree").stdout)
    for value in values:
        digest.update(value.encode())
        digest.update(b"\0")
    for relative in changed_paths(repo_dir):
        path = Path(repo_dir) / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        try:
            if path.is_symlink():
                digest.update(b"symlink:" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(str(path.stat().st_mode & 0o777).encode())
                digest.update(path.read_bytes())
            elif not path.exists():
                digest.update(b"deleted")
            else:
                raise DevopsError(f"unsupported changed path: {relative}")
        except OSError as e:
            raise DevopsError(f"cannot inspect changed path {relative}: {e}") from e
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_paths(repo_dir: Path, paths: list[str]) -> None:
    root = Path(repo_dir).resolve()
    for name in paths:
        relative = Path(name)
        full = root / relative
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise DevopsError(f"unsafe ticket path: {name}")
        if not full.parent.resolve().is_relative_to(root):
            raise DevopsError(f"ticket path escapes the workspace: {name}")


def approve_changes(repo_dir: Path, ticket: Ticket) -> None:
    ticket.changed_files = changed_paths(repo_dir)
    _validate_paths(repo_dir, ticket.changed_files)
    ticket.approved_digest = workspace_digest(repo_dir, include_index=False)
    ticket.approved_index = _git(repo_dir, "write-tree").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="issue-runner-index-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        _git(repo_dir, "read-tree", "HEAD", env=env)
        if ticket.changed_files:
            _git(repo_dir, "add", "--", *ticket.changed_files, env=env)
        ticket.approved_tree = _git(repo_dir, "write-tree", env=env).stdout.strip()


def recover_commit(repo_dir: Path, ticket: Ticket) -> str | None:
    if not ticket.commit_token or not ticket.base_commit:
        return None
    marker = f"Issue-Runner-Ticket: {ticket.commit_token}"
    sha = _git(
        repo_dir,
        "log",
        f"{ticket.base_commit}..HEAD",
        "--format=%H",
        "--fixed-strings",
        f"--grep={marker}",
        "-1",
    ).stdout.strip()
    if not sha:
        return None
    body = _git(repo_dir, "show", "-s", "--format=%B", sha).stdout
    parent = _git(repo_dir, "rev-parse", f"{sha}^").stdout.strip()
    tree = _git(repo_dir, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    if (
        marker not in body.splitlines()
        or sha != head_commit(repo_dir)
        or parent != ticket.base_commit
        or tree != ticket.approved_tree
    ):
        raise DevopsError("the recovered commit does not match the approved ticket")
    require_clean(repo_dir)
    return sha


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "change"


# junk a `git add -A` must never sweep into a ticket commit
# the run worktree's own environment and build byproducts are never ticket work
DEFAULT_EXCLUDES = (
    ".issue-runner/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".venv/",
    "node_modules/",
    "*.egg-info/",
)


# the team convention: feature/, bugfix/ and hotfix/ only. issue-<n>-* is the
# name earlier versions used; runs saved with it still resume and commit.
RUN_BRANCH_PREFIXES = ("feature/", "bugfix/", "hotfix/")
LEGACY_BRANCH_PREFIX = "issue-"


def _label_names(issue: dict) -> list[str]:
    names = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name.strip().lower())
    return names


def branch_kind(issue: dict) -> str:
    """`bugfix` for an issue labelled bug, otherwise `feature`."""
    return "bugfix" if "bug" in _label_names(issue) else "feature"


def branch_name(issue_ref: str, slug: str, kind: str = "feature") -> str:
    if f"{kind}/" not in RUN_BRANCH_PREFIXES:
        raise DevopsError(f"unknown branch kind {kind!r}")
    return f"{kind}/{issue_ref}-{slug}" if slug else f"{kind}/{issue_ref}"


def is_run_branch(branch: str) -> bool:
    return branch.startswith(RUN_BRANCH_PREFIXES + (LEGACY_BRANCH_PREFIX,))


def create_branch(repo_dir: Path, branch: str) -> str:
    require_clean(repo_dir)
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


def create_worktree(repo_dir: Path, branch: str, directory: Path) -> Path:
    require_clean(repo_dir)
    _git(repo_dir, "check-ref-format", "--branch", branch)
    if directory.exists():
        raise DevopsError(f"refusing to reuse an unrecorded worktree directory: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "worktree", "add", "--no-track", "-b", branch, str(directory), "HEAD")
    return directory.resolve()


def branch_exists(repo_dir: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode not in (0, 1):
        raise DevopsError(f"could not determine whether branch {branch} exists")
    return result.returncode == 0


def finish_worktree_creation(
    repo_dir: Path, branch: str, directory: Path, initial_head: str
) -> Path:
    if not directory.exists():
        if branch_exists(repo_dir, branch):
            if _git(repo_dir, "rev-parse", branch).stdout.strip() != initial_head:
                raise DevopsError("the pending run branch no longer matches its recorded base")
            directory.parent.mkdir(parents=True, exist_ok=True)
            _git(repo_dir, "worktree", "add", str(directory), branch)
        else:
            if head_commit(repo_dir) != initial_head:
                raise DevopsError("the source HEAD changed during worktree creation")
            create_worktree(repo_dir, branch, directory)
    if (
        git_common_dir(directory) != git_common_dir(repo_dir)
        or current_branch(directory) != branch
        or head_commit(directory) != initial_head
    ):
        raise DevopsError("the pending worktree does not match the recorded run")
    require_clean(directory)
    return directory.resolve()


def push_branch(repo_dir: Path, branch: str) -> None:
    """Publish the issue branch so a PR can be opened against it."""
    _git(repo_dir, "push", "-u", "origin", branch)


def commit_changes(repo_dir: Path, message: str, expected_branch: str) -> str:
    """Commit every current change on the run branch, after the runner's own checks."""
    branch = current_branch(repo_dir)
    if branch != expected_branch or not is_run_branch(branch):
        raise DevopsError(f"refusing to commit on {branch} — the run must be on {expected_branch}")
    paths = changed_paths(repo_dir)
    if not paths:
        raise DevopsError("nothing to commit")
    _validate_paths(repo_dir, paths)
    _git(repo_dir, "add", "-A", "--", *paths)
    _git(repo_dir, "commit", "-m", message)
    require_clean(repo_dir)
    return head_commit(repo_dir)


def fetch(repo_dir: Path) -> None:
    _git(repo_dir, "fetch", "--quiet", "origin")


def merge_in(repo_dir: Path, ref: str, message: str) -> list[str]:
    """Start merging `ref` into the current branch without committing.

    Returns the conflicted paths ([] for a clean merge). The merge stays in
    progress either way, so the runner can check it before `finish_merge`.
    """
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "-m", message, ref],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
    )
    conflicted = unmerged_paths(repo_dir)
    if result.returncode != 0 and not conflicted:
        raise DevopsError(
            f"git merge {ref} failed: {(result.stderr or result.stdout).strip()[:400]}"
        )
    if not merge_in_progress(repo_dir):
        raise DevopsError(f"{ref} is already merged; there is nothing to update")
    return conflicted


def unmerged_paths(repo_dir: Path) -> list[str]:
    out = _git(repo_dir, "diff", "--name-only", "--diff-filter=U", "-z").stdout
    return sorted({p for p in out.split("\0") if p})


def merge_in_progress(repo_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def leftover_markers(repo_dir: Path, paths: list[str]) -> list[str]:
    """Conflict markers git itself still sees in `paths`."""
    if not paths:
        return []
    result = subprocess.run(
        ["git", "diff", "--check", "HEAD", "--", *paths],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (result.stdout or "").splitlines()
    return [line for line in lines if "conflict marker" in line]


def finish_merge(repo_dir: Path, expected_branch: str) -> str:
    """Commit the in-progress merge with the message given to `merge_in`."""
    if current_branch(repo_dir) != expected_branch:
        raise DevopsError(f"refusing to finish a merge outside {expected_branch}")
    if not merge_in_progress(repo_dir):
        raise DevopsError("no merge is in progress")
    paths = changed_paths(repo_dir)
    _validate_paths(repo_dir, paths)
    _git(repo_dir, "add", "-A", "--", *paths)
    _git(repo_dir, "commit", "--no-edit")
    require_clean(repo_dir)
    return head_commit(repo_dir)


def abort_merge(repo_dir: Path) -> None:
    """Undo the runner's own in-progress merge; nothing else is discarded."""
    if merge_in_progress(repo_dir):
        _git(repo_dir, "merge", "--abort")


def contains(repo_dir: Path, commit: str, tip: str) -> bool | None:
    """Whether `tip` contains `commit`; None when either is not known locally."""
    if commit == tip:
        return True
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, tip],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return {0: True, 1: False}.get(result.returncode)


def files_in_commit(repo_dir: Path, commit: str) -> list[str]:
    """Paths a commit changed against its first parent (what a push of it delivers)."""
    out = _git(repo_dir, "diff", "--name-only", "--no-renames", "-z", f"{commit}^1", commit).stdout
    return sorted(p for p in out.split("\0") if p)


def is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def ensure_excluded(repo_dir: Path, pattern: str) -> None:
    """Keep runner state out of the target repo's commits without touching .gitignore."""
    raw = _git(repo_dir, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = Path(repo_dir) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if pattern not in existing.splitlines():
        exclude.write_text(existing.rstrip("\n") + f"\n{pattern}\n")


def commit_ticket(repo_dir: Path, ticket: Ticket, expected_branch: str | None = None) -> str:
    branch = current_branch(repo_dir)
    if not is_run_branch(branch) or (expected_branch is not None and branch != expected_branch):
        raise DevopsError(f"refusing to commit on {branch} — the run must be on its run branch")
    if ticket.base_commit is not None and head_commit(repo_dir) != ticket.base_commit:
        raise DevopsError("HEAD changed outside the ticket commit step; no changes were discarded")
    if ticket.approved_digest is None or ticket.approved_tree is None:
        raise DevopsError("ticket has no approved change set")
    if workspace_digest(repo_dir, include_index=False) != ticket.approved_digest:
        raise DevopsError("workspace changed after approval; refusing to stage it")
    if _git(repo_dir, "write-tree").stdout.strip() not in (
        ticket.approved_index,
        ticket.approved_tree,
    ):
        raise DevopsError("the index changed outside the approved commit step")
    _validate_paths(repo_dir, ticket.changed_files)
    if ticket.changed_files:
        _git(repo_dir, "add", "--", *ticket.changed_files)
    staged_paths = _git(repo_dir, "diff", "--cached", "--name-only", "--no-renames", "-z").stdout
    if set(filter(None, staged_paths.split("\0"))) != set(ticket.changed_files):
        raise DevopsError("the staged change set does not match the approved ticket")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(repo_dir), capture_output=True, check=False
    ).returncode
    if staged == 0:
        if ticket.already_satisfied:
            return head_commit(repo_dir)
        raise DevopsError(f"nothing to commit for ticket {ticket.id}")
    message = (
        f"feat(ticket-{ticket.id}): {ticket.title}\n\n"
        f"- assertion: {ticket.test_assertion}\n\n"
        "Co-authored with AI"
    )
    if ticket.commit_token:
        message += f"\n\nIssue-Runner-Ticket: {ticket.commit_token}"
    _git(repo_dir, "commit", "-m", message)
    sha = head_commit(repo_dir)
    if _git(repo_dir, "rev-parse", "HEAD^{tree}").stdout.strip() != ticket.approved_tree:
        raise DevopsError("a commit hook changed the approved tree; the commit was not accepted")
    require_clean(repo_dir)
    return sha


def _existed_at_head(repo_dir: Path, path: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{path}"],
            cwd=str(repo_dir),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _is_test_path(path: str) -> bool:
    name = Path(path).name
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    return "tests" in Path(path).parts[:-1]


def shared_changes(repo_dir: Path, changed_files: list[str], own_test: str) -> list[str]:
    """Changed files that existed at HEAD, are not tests, and are covered by another test."""
    shared: list[str] = []
    for path in changed_files:
        if _is_test_path(path) or not _existed_at_head(repo_dir, path):
            continue
        if any(test != own_test for test in related_tests(repo_dir, [path])):
            shared.append(path)
    return sorted(shared)
