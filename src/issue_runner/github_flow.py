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

    def api(self, path: str, method: str = "GET", body: dict | None = None, raw: bool = False):
        """Call the REST (or GraphQL) API and return the decoded JSON (or text), or None."""
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
        if raw:
            return result.stdout or ""
        return json.loads(text) if text else None

    def graphql(self, query: str, **variables) -> dict:
        data = self.api("graphql", "POST", {"query": query, "variables": variables})
        if not isinstance(data, dict) or data.get("errors"):
            errors = (data or {}).get("errors") if isinstance(data, dict) else data
            raise GithubError(f"GraphQL failed: {str(errors)[:400]}")
        return data["data"]

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

    # --- pull request and review ---

    def open_pull(self, branch: str) -> dict | None:
        owner = self.repo.split("/")[0]
        pulls = self.api(f"repos/{{repo}}/pulls?head={owner}:{branch}&state=open&per_page=10")
        return pulls[0] if pulls else None

    def create_pull(self, title: str, head: str, base: str, body: str) -> dict:
        return self.api(
            "repos/{repo}/pulls",
            "POST",
            {"title": title, "head": head, "base": base, "body": body, "draft": False},
        )

    def pull(self, number: int) -> dict:
        return self.api(f"repos/{{repo}}/pulls/{number}")

    def reviews(self, number: int) -> list[dict]:
        return self.api(f"repos/{{repo}}/pulls/{number}/reviews?per_page=100") or []

    def review_threads(self, number: int) -> list[dict]:
        owner, name = self.repo.split("/", 1)
        data = self.graphql(_THREADS_QUERY, owner=owner, name=name, number=number)
        return data["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    def check_runs(self, sha: str) -> list[dict]:
        data = self.api(f"repos/{{repo}}/commits/{sha}/check-runs?per_page=100") or {}
        return data.get("check_runs", [])

    def statuses(self, sha: str) -> list[dict]:
        data = self.api(f"repos/{{repo}}/commits/{sha}/status") or {}
        return data.get("statuses", [])

    def request_review(self, number: int, reviewer: str) -> None:
        self.api(
            f"repos/{{repo}}/pulls/{number}/requested_reviewers", "POST", {"reviewers": [reviewer]}
        )

    def reply_to_thread(self, thread_id: str, body: str) -> None:
        self.graphql(_REPLY_MUTATION, thread=thread_id, body=body)

    def resolve_thread(self, thread_id: str) -> None:
        self.graphql(_RESOLVE_MUTATION, thread=thread_id)

    def comment(self, number: int, body: str) -> None:
        self.api(f"repos/{{repo}}/issues/{number}/comments", "POST", {"body": body})

    def job_log(self, job_id: int) -> str:
        return self.api(f"repos/{{repo}}/actions/jobs/{job_id}/logs", raw=True)


_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated path line
          comments(first: 20) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""

_REPLY_MUTATION = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { id }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}
"""
