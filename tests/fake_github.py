"""An in-memory GitHub for --deploy tests, driven through `gh api` argv.

GitHubFlow takes a `run` callable shaped like subprocess.run. FakeGitHub is
that callable: it decodes `gh api --method M PATH [--input -]`, routes the call
to the state below and answers like gh would, including `HTTP 404` on stderr.
Tests mutate the public attributes to build each scenario, and read `calls`
to assert what the runner did (and did not) ask GitHub for.
"""

import base64
import json
import re
import subprocess
from dataclasses import dataclass, field

WORKFLOW_PATH = ".github/workflows/dev-deployment.yaml"
DEPLOY_WORKFLOW = """name: dev-deployment
on:
  push:
    branches:
      - main
    paths:
      - "src/**"
  workflow_dispatch:
"""


class _NotFound(Exception):
    pass


BOT = "copilot-pull-request-reviewer[bot]"
APPROVE = "🟢 Approval recommended"
CHANGES = "🟡 Changes recommended"


@dataclass
class Head:
    """What GitHub reports for one pushed head: CI, the Copilot review, its threads."""

    checks: list = field(default_factory=lambda: [("ci", "success")])
    verdict: str | None = APPROVE  # None: no review ever arrives
    summary: str = "Looks fine."
    threads: list = field(default_factory=list)  # (path, line, body)
    statuses: list = field(default_factory=list)  # (context, state)
    polls_until_done: int = 0  # pull() calls before checks finish and the review lands


class FakeGitHub:
    def __init__(self, repo: str = "o/n"):
        self.repo = repo
        self.calls: list[tuple[str, str, object]] = []
        self.repo_data = {
            "full_name": repo,
            "default_branch": "main",
            "permissions": {"push": True, "admin": False},
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
        }
        self.rules: dict[str, list[dict]] = {
            "main": [
                {
                    "type": "copilot_code_review",
                    "parameters": {"review_on_push": False, "review_draft_pull_requests": True},
                }
            ]
        }
        self.protection: dict[str, dict] = {}
        self.workflows = {
            "dev-deployment.yaml": {"id": 11, "path": WORKFLOW_PATH, "state": "active"}
        }
        self.files = {(WORKFLOW_PATH, "main"): DEPLOY_WORKFLOW}
        self.environments = {"development"}
        self.errors: dict[str, str] = {}  # path prefix -> stderr for a forced failure
        # pull requests: a bare git remote is the source of truth for head SHAs
        self.remote = None
        self.review_on_push = False
        self.scenarios: list[Head] = []  # consumed, in order, by each new head
        self.pulls: dict[int, dict] = {}
        self.heads: dict[str, dict] = {}  # sha -> {"head": Head, "polls": int, "reviewed": bool}
        self.reviews: dict[int, list] = {}
        self.threads: dict[str, dict] = {}
        self.thread_replies: dict[str, list] = {}
        self.review_requests: list[tuple[int, str]] = []
        self.pr_comments: list[tuple[int, str]] = []
        self.external_push = None  # set to a SHA to make the PR head move outside the runner

    # --- subprocess.run stand-in ---

    def __call__(self, argv, capture_output=True, text=True, input=None, **kwargs):
        assert argv[:3] == ["gh", "api", "--method"], argv
        method, path = argv[3], argv[4]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        for prefix, stderr in self.errors.items():
            if path.startswith(prefix):
                return subprocess.CompletedProcess(argv, 1, "", stderr)
        try:
            result = self._route(method, path, body)
        except _NotFound:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        out = "" if result is None else json.dumps(result)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def paths(self, method: str = "GET") -> list[str]:
        return [p for m, p, _ in self.calls if m == method]

    # --- routing ---

    def _route(self, method: str, path: str, body):
        if path == "graphql":
            return self._graphql(body)
        bare, _, query = path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        prefix = f"repos/{self.repo}"
        if not bare.startswith(prefix):
            raise _NotFound
        rest = bare[len(prefix) :]
        for pattern, handler in self._handlers():
            match = re.fullmatch(pattern.removesuffix("#post"), rest)
            if match and handler[0] == method:
                return handler[1](*match.groups(), params=params, body=body)
        raise _NotFound

    def _handlers(self):
        return [
            (r"", ("GET", lambda **_: self.repo_data)),
            (r"/rules/branches/([^/]+)", ("GET", lambda b, **_: self.rules.get(b, []))),
            (r"/branches/([^/]+)/protection", ("GET", self._protection)),
            (r"/actions/workflows/([^/]+)", ("GET", self._workflow)),
            (r"/contents/(.+)", ("GET", self._contents)),
            (r"/environments/([^/]+)", ("GET", self._environment)),
            (r"/pulls", ("GET", self._list_pulls)),
            (r"/pulls#post", ("POST", self._create_pull)),
            (r"/pulls/(\d+)", ("GET", self._pull)),
            (r"/pulls/(\d+)/reviews", ("GET", self._reviews)),
            (r"/pulls/(\d+)/requested_reviewers", ("POST", self._request_review)),
            (r"/commits/([0-9a-f]+)/check-runs", ("GET", self._check_runs)),
            (r"/commits/([0-9a-f]+)/status", ("GET", self._status)),
            (r"/issues/(\d+)/comments", ("POST", self._comment)),
        ]

    def _protection(self, branch, **_):
        if branch not in self.protection:
            raise _NotFound
        return self.protection[branch]

    def _workflow(self, name, **_):
        if name not in self.workflows:
            raise _NotFound
        return self.workflows[name]

    def _contents(self, path, params, **_):
        key = (path, params.get("ref", "main"))
        if key not in self.files:
            raise _NotFound
        content = base64.b64encode(self.files[key].encode()).decode()
        # GitHub wraps base64 at 60 characters
        wrapped = "\n".join(content[i : i + 60] for i in range(0, len(content), 60))
        return {"encoding": "base64", "content": wrapped, "path": path}

    def _environment(self, name, **_):
        if name not in self.environments:
            raise _NotFound
        return {"name": name}

    # --- pull requests and review ---

    def remote_head(self, branch: str) -> str:
        if self.external_push:
            return self.external_push
        out = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    def _see(self, number: int, sha: str) -> dict:
        """First sight of a head: take the next scenario for it."""
        if sha not in self.heads:
            first = not any(h["pr"] == number for h in self.heads.values())
            self.heads[sha] = {
                "pr": number,
                "head": self.scenarios.pop(0) if self.scenarios else Head(),
                "polls": 0,
                "due": first or self.review_on_push,
                "reviewed": False,
            }
        return self.heads[sha]

    def _settle(self, number: int, sha: str) -> None:
        seen = self.heads[sha]
        if seen["polls"] < seen["head"].polls_until_done:
            return
        if seen["due"] and not seen["reviewed"] and seen["head"].verdict is not None:
            seen["reviewed"] = True
            review_id = sum(len(r) for r in self.reviews.values()) + 1
            self.reviews.setdefault(number, []).append(
                {
                    "id": review_id,
                    "user": {"login": BOT, "type": "Bot"},
                    "state": "COMMENTED",
                    "commit_id": sha,
                    "body": f"<!-- ccr-overview-v2 -->\n\n## Copilot review overview\n\n"
                    f"### {seen['head'].verdict}\n\n{seen['head'].summary}\n\n"
                    "**Findings:** None\n",
                }
            )
            for path, line, text in seen["head"].threads:
                thread_id = f"PRRT_{len(self.threads) + 1}"
                self.threads[thread_id] = {
                    "pr": number,
                    "id": thread_id,
                    "isResolved": False,
                    "isOutdated": False,
                    "path": path,
                    "line": line,
                    "comments": {
                        "nodes": [{"author": {"login": BOT.removesuffix("[bot]")}, "body": text}]
                    },
                }

    def _list_pulls(self, params, **_):
        ref = params.get("head", "").split(":", 1)[-1]
        return [
            self._pull(str(n), count=False)
            for n, pr in self.pulls.items()
            if pr["head"]["ref"] == ref and pr["state"] == "open"
        ]

    def _create_pull(self, body, **_):
        number = 100 + len(self.pulls) + 1
        self.pulls[number] = {
            "number": number,
            "state": "open",
            "merged": False,
            "title": body["title"],
            "body": body["body"],
            "draft": body.get("draft"),
            "head": {"ref": body["head"], "sha": None},
            "base": {"ref": body["base"]},
            "html_url": f"https://github.com/{self.repo}/pull/{number}",
        }
        return self._pull(str(number), count=False)

    def _pull(self, number, count=True, **_):
        """A PR read. Only a runner poll (count=True) moves the head's scenario along."""
        number = int(number)
        if number not in self.pulls:
            raise _NotFound
        pr = self.pulls[number]
        sha = self.remote_head(pr["head"]["ref"])
        pr["head"]["sha"] = sha
        if sha in self.heads or not self.external_push:
            seen = self._see(number, sha)
            seen["polls"] += 1 if count else 0
            self._settle(number, sha)
        return dict(pr)

    def _reviews(self, number, **_):
        return list(self.reviews.get(int(number), []))

    def _request_review(self, number, body, **_):
        number = int(number)
        for reviewer in body["reviewers"]:
            self.review_requests.append((number, reviewer))
        sha = self.remote_head(self.pulls[number]["head"]["ref"])
        seen = self._see(number, sha)
        if BOT in body["reviewers"]:
            seen["due"] = True
            self._settle(number, sha)
        return {}

    def _check_runs(self, sha, **_):
        seen = self.heads.get(sha)
        if seen is None:
            return {"check_runs": []}
        done = seen["polls"] >= seen["head"].polls_until_done
        return {
            "check_runs": [
                {
                    "id": i,
                    "name": name,
                    "status": "completed" if done else "in_progress",
                    "conclusion": conclusion if done else None,
                    "output": {"title": f"{name} {conclusion}"},
                }
                for i, (name, conclusion) in enumerate(seen["head"].checks, start=1)
            ]
        }

    def _status(self, sha, **_):
        seen = self.heads.get(sha)
        statuses = seen["head"].statuses if seen else []
        return {"statuses": [{"context": c, "state": st} for c, st in statuses]}

    def _comment(self, number, body, **_):
        self.pr_comments.append((int(number), body["body"]))
        return {"id": len(self.pr_comments)}

    def _graphql(self, body):
        query, variables = body["query"], body["variables"]
        if "reviewThreads" in query:
            nodes = [
                {k: v for k, v in t.items() if k != "pr"}
                for t in self.threads.values()
                if t["pr"] == variables["number"]
            ]
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
        if "addPullRequestReviewThreadReply" in query:
            self.thread_replies.setdefault(variables["thread"], []).append(variables["body"])
            return {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "c"}}}}
        if "resolveReviewThread" in query:
            self.threads[variables["thread"]]["isResolved"] = True
            return {"data": {"resolveReviewThread": {"thread": {"id": variables["thread"]}}}}
        return {"errors": [{"message": "unsupported query"}]}
