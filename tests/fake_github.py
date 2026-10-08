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


class _HttpError(Exception):
    """Any other error status, reported the way gh prints it."""


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


@dataclass
class Deploy:
    """What the deploy workflow does when a push to the base (or a dispatch) starts it."""

    run_conclusion: str = "success"
    deployment_state: str | None = "success"  # None: the run records no deployment
    polls: int = 1  # reads of the run list before the run completes
    failed_job: str = "Deploy to Development / Deploy Application"
    supersede: "Deploy | None" = None  # when cancelled, a newer main commit gets this deploy
    triggered: bool = True  # False: the push does not start the workflow (paths filter)


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
        self.require_up_to_date = False  # branch protection "require branches to be up to date"
        self.merge_calls: list[dict] = []
        self.refuse_merges = 0  # answer 405 to this many merge attempts
        self.refuse_body_updates = 0  # answer 502 to this many PR body updates
        self.on_review_request = None  # called after each review request, e.g. to move main
        # deployment
        self.deploys: list[Deploy] = []  # consumed, in order, by each push to main or dispatch
        self.runs: list[dict] = []
        self.deployment_list: list[dict] = []
        self.dispatches: list[dict] = []
        self.on_deployed = None  # called when a deployment succeeds, e.g. to make "Dev" change
        self.issues: dict[int, dict] = {}  # number -> {"body", "state", "state_reason"}

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
        except _HttpError as e:
            return subprocess.CompletedProcess(argv, 1, "", f"gh: {e}")
        out = "" if result is None else result if isinstance(result, str) else json.dumps(result)
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
            match = re.fullmatch(pattern.split("#")[0], rest)
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
            (r"/pulls/(\d+)#patch", ("PATCH", self._update_pull)),
            (r"/pulls/(\d+)/reviews", ("GET", self._reviews)),
            (r"/pulls/(\d+)/requested_reviewers", ("POST", self._request_review)),
            (r"/pulls/(\d+)/merge", ("PUT", self._merge)),
            (r"/commits/([0-9a-f]+)/check-runs", ("GET", self._check_runs)),
            (r"/commits/([0-9a-f]+)/status", ("GET", self._status)),
            (r"/issues/(\d+)/comments", ("POST", self._comment)),
            (r"/issues/(\d+)", ("GET", self._issue)),
            (r"/issues/(\d+)#patch", ("PATCH", self._update_issue)),
            (r"/actions/workflows/([^/]+)/runs", ("GET", self._runs)),
            (r"/actions/workflows/([^/]+)/dispatches", ("POST", self._dispatch)),
            (r"/actions/runs/(\d+)/jobs", ("GET", self._jobs)),
            (r"/actions/jobs/(\d+)/logs", ("GET", self._job_log)),
            (r"/deployments", ("GET", self._deployments)),
            (r"/deployments/(\d+)/statuses", ("GET", self._deployment_statuses)),
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
        if pr["merged"]:
            return dict(pr)
        sha = self.remote_head(pr["head"]["ref"])
        pr["head"]["sha"] = sha
        pr["mergeable_state"] = self._merge_state(pr["base"]["ref"], sha)
        pr["mergeable"] = pr["mergeable_state"] != "dirty"
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
        if self.on_review_request:
            self.on_review_request()
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

    # --- merging ---

    def _git(self, *args, check=True):
        return subprocess.run(
            ["git", "--git-dir", str(self.remote), *args],
            capture_output=True,
            text=True,
            check=check,
            env={
                "GIT_AUTHOR_NAME": "gh",
                "GIT_AUTHOR_EMAIL": "gh@x",
                "GIT_COMMITTER_NAME": "gh",
                "GIT_COMMITTER_EMAIL": "gh@x",
                "PATH": "/usr/bin:/bin",
            },
        )

    def _merge_state(self, base: str, head: str) -> str:
        base_tip = self._tip(base)
        if self._git("merge-base", "--is-ancestor", base_tip, head, check=False).returncode == 0:
            return "clean"
        trial = self._git("merge-tree", "--write-tree", base_tip, head, check=False)
        if trial.returncode != 0:
            return "dirty"
        return "behind" if self.require_up_to_date else "clean"

    def _tip(self, branch: str) -> str:
        return self._git("rev-parse", f"refs/heads/{branch}").stdout.strip()

    def _merge(self, number, body, **_):
        number = int(number)
        pr = self.pulls[number]
        self.merge_calls.append(dict(body, number=number))
        head = self._tip(pr["head"]["ref"])
        if body.get("sha") != head:
            raise _HttpError("Head branch was modified. Review and try the merge again. (HTTP 409)")
        if self.refuse_merges:
            self.refuse_merges -= 1
            raise _HttpError("Pull Request is not mergeable (HTTP 405)")
        base = pr["base"]["ref"]
        state = self._merge_state(base, head)
        if state != "clean":
            raise _HttpError(f"Pull Request is not mergeable: {state} (HTTP 405)")
        base_tip = self._tip(base)
        tree = self._git("merge-tree", "--write-tree", base_tip, head).stdout.split()[0]
        title = body.get("commit_title") or pr["title"]
        parents = ["-p", base_tip] + (["-p", head] if body["merge_method"] == "merge" else [])
        merged = self._git("commit-tree", tree, *parents, "-m", title).stdout.strip()
        self._git("update-ref", f"refs/heads/{base}", merged, base_tip)
        pr.update(merged=True, state="closed", merge_commit_sha=merged)
        self._start_deploy(merged, "push")
        return {"merged": True, "sha": merged, "message": "Pull Request successfully merged"}

    # --- deployment ---

    def _start_deploy(self, sha: str, event: str) -> None:
        spec = self.deploys.pop(0) if self.deploys else Deploy()
        if event == "push" and not spec.triggered:
            return
        run_id = 9000 + len(self.runs) + 1
        self.runs.insert(
            0,
            {
                "id": run_id,
                "head_sha": sha,
                "event": event,
                "status": "queued",
                "conclusion": None,
                "html_url": f"https://github.com/{self.repo}/actions/runs/{run_id}",
                "_spec": spec,
                "_reads": 0,
            },
        )

    def _runs(self, workflow, **_):
        for run in list(self.runs):
            if run["status"] == "completed":
                continue
            run["_reads"] += 1
            if run["_reads"] >= run["_spec"].polls:
                self._finish_run(run)
        return {
            "workflow_runs": [
                {k: v for k, v in r.items() if not k.startswith("_")} for r in self.runs
            ]
        }

    def _finish_run(self, run: dict) -> None:
        spec = run["_spec"]
        run.update(status="completed", conclusion=spec.run_conclusion)
        if spec.run_conclusion == "success" and spec.deployment_state:
            deployment_id = 7000 + len(self.deployment_list) + 1
            self.deployment_list.insert(
                0,
                {
                    "id": deployment_id,
                    "sha": run["head_sha"],
                    "environment": "development",
                    "_statuses": [
                        {"state": spec.deployment_state, "log_url": run["html_url"]},
                        {"state": "in_progress", "log_url": run["html_url"]},
                    ],
                },
            )
            if spec.deployment_state == "success" and self.on_deployed:
                self.on_deployed()
        if spec.run_conclusion == "cancelled" and spec.supersede is not None:
            tip = self._tip("main")
            tree = self._git("rev-parse", f"{tip}^{{tree}}").stdout.strip()
            newer = self._git("commit-tree", tree, "-p", tip, "-m", "feat: teammate").stdout.strip()
            self._git("update-ref", "refs/heads/main", newer, tip)
            self.deploys.insert(0, spec.supersede)
            self._start_deploy(newer, "push")

    def _dispatch(self, workflow, body, **_):
        self.dispatches.append({"workflow": workflow, **body})
        self._start_deploy(self._tip(body["ref"]), "workflow_dispatch")

    def _jobs(self, run_id, **_):
        run = next(r for r in self.runs if r["id"] == int(run_id))
        spec = run["_spec"]
        jobs = [{"id": 1, "name": "CI / CI", "conclusion": "success"}]
        if run["conclusion"] == "failure":
            jobs.append({"id": 2, "name": spec.failed_job, "conclusion": "failure"})
        return {"jobs": jobs}

    def _job_log(self, job_id, **_):
        return "step 1 ok\nError: the stack update failed\n"

    def _deployments(self, params, **_):
        env = params.get("environment")
        return [
            {k: v for k, v in d.items() if not k.startswith("_")}
            for d in self.deployment_list
            if d["environment"] == env
        ]

    def _deployment_statuses(self, deployment_id, **_):
        d = next(d for d in self.deployment_list if d["id"] == int(deployment_id))
        return list(d["_statuses"])

    # --- the issue ---

    def _issue(self, number, **_):
        number = int(number)
        if number not in self.issues:
            raise _NotFound
        return {"number": number, **self.issues[number]}

    def _update_pull(self, number, body, **_):
        if self.refuse_body_updates > 0:
            self.refuse_body_updates -= 1
            raise _HttpError("Server Error (HTTP 502)")
        pr = self.pulls[int(number)]
        pr.update(body)
        return dict(pr)

    def _update_issue(self, number, body, **_):
        issue = self.issues[int(number)]
        issue.update(body)
        return {"number": int(number), **issue}

    def issue_comments(self, number: int) -> list[str]:
        return [body for n, body in self.pr_comments if n == number]
