"""GitHub I/O via the gh CLI: issue reads and sub-task ticket mirroring.

None of this costs model credits. `--issue-file` (issue_from_file) allows fully
offline/end-to-end runs with no GitHub repo at all.
"""

import json
import re
import subprocess
from pathlib import Path


class GithubError(RuntimeError):
    pass


def _run(argv, run=subprocess.run):
    result = run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise GithubError(f"{' '.join(argv[:3])} failed: {(result.stderr or '').strip()[:400]}")
    return result.stdout


def fetch_issue(issue_ref: str, repo: str | None = None, run=subprocess.run) -> dict:
    argv = ["gh", "issue", "view", str(issue_ref), "--json", "number,title,body,url"]
    if repo:
        argv += ["-R", repo]
    return json.loads(_run(argv, run))


def issue_from_file(path: Path) -> dict:
    lines = Path(path).read_text().strip().splitlines()
    title = lines[0].lstrip("# ").strip() if lines else "untitled"
    body = "\n".join(lines[1:]).strip()
    return {"number": 0, "title": title, "body": body, "url": f"file://{path}"}


def create_subissue(repo: str, parent_number: int, ticket, run=subprocess.run) -> int:
    body = (
        f"Part of #{parent_number}.\n\n"
        f"{ticket.description}\n\n"
        f"**Single test assertion:** `{ticket.test_assertion}`"
    )
    return create_issue(repo, f"[#{parent_number}] {ticket.title}", body, run=run)["number"]


def create_issue(repo: str, title: str, body: str, run=subprocess.run) -> dict:
    """Create an issue without modifying its supplied title or body."""
    argv = [
        "gh",
        "issue",
        "create",
        "-R",
        repo,
        "--title",
        title,
        "--body",
        body,
    ]
    stdout = _run(argv, run)
    match = re.search(r"/issues/(\d+)", stdout)
    if not match:
        raise GithubError(f"could not parse issue number from gh output: {stdout[:200]!r}")
    number = int(match.group(1))
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/{repo}/issues/{number}",
    }


def comment_issue(repo: str, number: int, body: str, run=subprocess.run) -> None:
    _run(["gh", "issue", "comment", "-R", repo, str(number), "--body", body], run)


def close_subissue(repo: str, number: int, comment: str, run=subprocess.run) -> None:
    """Close a finished sub-task, recorded as genuinely completed.

    The reason matters: GitHub renders "completed" and "not planned" with
    different icons, so without it a proven, committed ticket is indistinguishable
    from one that was abandoned.
    """
    _run(
        [
            "gh",
            "issue",
            "close",
            "-R",
            repo,
            str(number),
            "--reason",
            "completed",
            "--comment",
            comment,
        ],
        run,
    )


def ensure_label(repo: str, label: str, color: str, description: str, run=subprocess.run) -> None:
    """Create the label if the repo does not have it yet.

    `--force` makes this idempotent: the first run creates it, later runs update
    it in place rather than failing on a duplicate.
    """
    _run(
        [
            "gh",
            "label",
            "create",
            label,
            "-R",
            repo,
            "--color",
            color,
            "--description",
            description,
            "--force",
        ],
        run,
    )


def add_label(repo: str, number: int, label: str, run=subprocess.run) -> None:
    _run(["gh", "issue", "edit", str(number), "-R", repo, "--add-label", label], run)


def remove_label(repo: str, number: int, label: str, run=subprocess.run) -> None:
    _run(["gh", "issue", "edit", str(number), "-R", repo, "--remove-label", label], run)


def list_subissues(repo: str, parent_number: int, run=subprocess.run) -> list[int]:
    """Numbers of the sub-issues this runner opened for `parent_number`."""
    prefix = f"[#{parent_number}] "
    argv = [
        "gh",
        "issue",
        "list",
        "-R",
        repo,
        "--search",
        f"{prefix} in:title",
        "--state",
        "all",
        "--limit",
        "100",
        "--json",
        "number,title",
    ]
    found = json.loads(_run(argv, run) or "[]")
    return [i["number"] for i in found if i["title"].startswith(prefix)]


def delete_issue(repo: str, number: int, run=subprocess.run) -> None:
    _run(["gh", "issue", "delete", "-R", repo, str(number), "--yes"], run)


def open_pull_request(repo: str, head: str, title: str, body: str, run=subprocess.run) -> str:
    """Open a PR for `head` against the repo's default base branch; return its URL."""
    argv = [
        "gh",
        "pr",
        "create",
        "-R",
        repo,
        "--head",
        head,
        "--title",
        title,
        "--body",
        body,
    ]
    stdout = _run(argv, run)
    match = re.search(r"https?://\S+", stdout)
    if not match:
        raise GithubError(f"could not parse a PR URL from gh output: {stdout[:200]!r}")
    return match.group(0)
