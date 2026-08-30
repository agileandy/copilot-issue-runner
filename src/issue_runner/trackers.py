"""Issue-source detection: GitHub via gh, or a self-hosted Gitea via its REST API.

The tracker is inferred from the target repo's `origin` remote — a github.com
remote uses `gh`; anything else is treated as Gitea, with the API base derived
from an http(s) remote URL or overridden by the GITEA_URL environment variable
(required for ssh remotes, whose port is not the API port). Auth: GITEA_TOKEN.
"""

import json
import logging
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class TrackerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteInfo:
    kind: str  # "github" | "gitea"
    api_base: str | None  # scheme://host[:port] for the Gitea API; None if underivable
    owner_repo: str


_HTTP_RE = re.compile(r"^(https?)://([^/]+)/(.+?)(?:\.git)?/?$")
_SCP_RE = re.compile(r"^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?$")
_SSH_RE = re.compile(r"^ssh://[\w.-]+@([\w.-]+)(?::\d+)?/(.+?)(?:\.git)?$")


def parse_remote(url: str) -> RemoteInfo:
    if m := _HTTP_RE.match(url):
        scheme, host, path = m.groups()
        kind = "github" if host.removeprefix("www.") == "github.com" else "gitea"
        return RemoteInfo(kind, f"{scheme}://{host}", path)
    if m := _SCP_RE.match(url):
        host, path = m.groups()
        kind = "github" if host.removeprefix("www.") == "github.com" else "gitea"
        return RemoteInfo(kind, None, path)
    if m := _SSH_RE.match(url):
        host, path = m.groups()
        kind = "github" if host.removeprefix("www.") == "github.com" else "gitea"
        return RemoteInfo(kind, None, path)
    raise TrackerError(f"cannot parse origin remote URL: {url!r}")


def resolve(repo_dir: Path) -> RemoteInfo:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TrackerError(
            f"no origin remote in {repo_dir} — pass --repo owner/name (GitHub) or --issue-file"
        )
    return parse_remote(result.stdout.strip())


def _http_get(url: str, token: str) -> str:
    request = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode()


def fetch_gitea_issue(
    api_base: str | None,
    owner_repo: str,
    number: str,
    token: str | None = None,
    getter=_http_get,
) -> dict:
    token = token if token is not None else os.environ.get("GITEA_TOKEN")
    if not token:
        raise TrackerError("GITEA_TOKEN is not set — export it to read Gitea issues")
    api_base = api_base or os.environ.get("GITEA_URL")
    if not api_base:
        raise TrackerError(
            "cannot derive the Gitea API base from an ssh remote — set GITEA_URL "
            "(e.g. http://gitea.example.com:3000)"
        )
    url = f"{api_base.rstrip('/')}/api/v1/repos/{owner_repo}/issues/{number}"
    data = json.loads(getter(url, token))
    return {
        "number": data["number"],
        "title": data["title"],
        "body": data.get("body") or "",
        "url": data.get("html_url", ""),
    }


def gitea_write_token() -> str:
    """Issue writes act as the claude bot; the human token is a warned fallback."""
    token = os.environ.get("GITEA_CLAUDE_TOKEN")
    if token:
        return token
    token = os.environ.get("GITEA_TOKEN")
    if token:
        logging.getLogger("issue_runner").warning(
            "GITEA_CLAUDE_TOKEN not set — Gitea ticket writes will be attributed "
            "to the human account, not the claude bot"
        )
        return token
    raise TrackerError("neither GITEA_CLAUDE_TOKEN nor GITEA_TOKEN is set")


def _http_json(method: str, url: str, token: str, payload: dict | None = None) -> str:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode()


def create_gitea_subissue(
    api_base: str,
    owner_repo: str,
    parent_number: int,
    ticket,
    token: str | None = None,
    requester=_http_json,
) -> int:
    token = token or gitea_write_token()
    body = (
        f"Part of #{parent_number}.\n\n"
        f"{ticket.description}\n\n"
        f"**Single test assertion:** `{ticket.test_assertion}`"
    )
    url = f"{api_base.rstrip('/')}/api/v1/repos/{owner_repo}/issues"
    reply = requester(
        "POST", url, token, {"title": f"[#{parent_number}] {ticket.title}", "body": body}
    )
    return int(json.loads(reply)["number"])


def close_gitea_issue(
    api_base: str,
    owner_repo: str,
    number: int,
    comment: str,
    token: str | None = None,
    requester=_http_json,
) -> None:
    token = token or gitea_write_token()
    base = f"{api_base.rstrip('/')}/api/v1/repos/{owner_repo}/issues/{number}"
    requester("POST", f"{base}/comments", token, {"body": comment})
    requester("PATCH", base, token, {"state": "closed"})
