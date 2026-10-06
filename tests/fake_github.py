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
        bare, _, query = path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        prefix = f"repos/{self.repo}"
        if not bare.startswith(prefix):
            raise _NotFound
        rest = bare[len(prefix) :]
        for pattern, handler in self._handlers():
            match = re.fullmatch(pattern, rest)
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
