"""GitHub reads and writes for --deploy runs, through `gh api`.

Every call goes through one injectable `run` (subprocess.run by default), the
same seam github_io uses, so tests drive the whole delivery flow against an
in-memory GitHub without a network.
"""

import base64
import json
import subprocess

from .github_io import GithubError


class NotFound(GithubError):
    """GitHub answered 404."""


class GitHubFlow:
    def __init__(self, repo: str, run=subprocess.run):
        self.repo = repo
        self.run = run

    def api(self, path: str, method: str = "GET", body: dict | None = None):
        """Call the REST (or GraphQL) API and return the decoded JSON, or None."""
        path = path.replace("{repo}", self.repo)
        argv = ["gh", "api", "--method", method, path]
        kwargs = {"capture_output": True, "text": True}
        if body is not None:
            argv += ["--input", "-"]
            kwargs["input"] = json.dumps(body)
        try:
            result = self.run(argv, **kwargs)
        except OSError as e:
            raise GithubError(f"could not run gh: {e}") from e
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if "HTTP 404" in message:
                raise NotFound(f"{method} {path}: not found")
            raise GithubError(f"gh api {method} {path} failed: {message[:400]}")
        text = (result.stdout or "").strip()
        return json.loads(text) if text else None

    def get_or_none(self, path: str):
        try:
            return self.api(path)
        except NotFound:
            return None

    # --- repository policy (preflight) ---

    def repository(self) -> dict:
        return self.api("repos/{repo}")

    def branch_rules(self, branch: str) -> list[dict]:
        """Every active ruleset rule on a branch, repo and org rulesets alike."""
        return self.api(f"repos/{{repo}}/rules/branches/{branch}?per_page=100") or []

    def branch_protection(self, branch: str) -> dict | None:
        """Classic branch protection, or None when the branch has none."""
        return self.get_or_none(f"repos/{{repo}}/branches/{branch}/protection")

    def workflow(self, name: str) -> dict | None:
        return self.get_or_none(f"repos/{{repo}}/actions/workflows/{name}")

    def file_text(self, path: str, ref: str) -> str | None:
        data = self.get_or_none(f"repos/{{repo}}/contents/{path}?ref={ref}")
        if not data or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def environment(self, name: str) -> dict | None:
        return self.get_or_none(f"repos/{{repo}}/environments/{name}")
