"""Preflight for --deploy: prove the repository can take a run to Dev before
any model call spends credits.

Everything here is a read. A run that could never satisfy the Definition of
Done (no acceptance criteria, no deploy workflow, no way to merge) stops with
exit 2 and the full list of reasons, rather than after an hour of work.
"""

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .. import criteria as criteria_mod
from ..config import RunnerConfig
from ..github_flow import GithubError, GitHubFlow

_MERGE_FLAGS = (
    ("squash", "allow_squash_merge"),
    ("merge", "allow_merge_commit"),
    ("rebase", "allow_rebase_merge"),
)


class PreflightError(RuntimeError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("--deploy preflight failed:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass
class PushTrigger:
    branches: list[str] | None = None
    branches_ignore: list[str] | None = None
    paths: list[str] | None = None
    paths_ignore: list[str] | None = None

    def runs_on(self, branch: str) -> bool:
        if self.branches is not None:
            return any(_glob(branch, p) for p in self.branches)
        if self.branches_ignore is not None:
            return not any(_glob(branch, p) for p in self.branches_ignore)
        return True

    def fires_for(self, files: list[str]) -> bool:
        """Whether a push changing `files` passes the workflow's paths filters."""
        if self.paths is not None:
            return any(_glob(f, p) for f in files for p in self.paths)
        if self.paths_ignore is not None:
            return any(not any(_glob(f, p) for p in self.paths_ignore) for f in files)
        return True


@dataclass
class Preflight:
    repo: str
    default_branch: str
    merge_method: str
    review_source: str  # "ruleset" (GitHub requests it) or "request" (the runner does)
    review_on_push: bool
    required_approvals: int
    required_checks: list[str]
    workflow_path: str
    trigger: PushTrigger
    environment: str
    criteria: list = field(default_factory=list)

    def lines(self) -> list[str]:
        review = (
            "Copilot review from a ruleset"
            + (", re-reviews on push" if self.review_on_push else ", re-review requested by runner")
            if self.review_source == "ruleset"
            else "Copilot review requested by the runner (no ruleset found)"
        )
        paths = (
            f" when {', '.join(self.trigger.paths)} change"
            if self.trigger.paths
            else " on every push"
        )
        return [
            f"deploy preflight passed for {self.repo}",
            f"  merge: {self.merge_method} into {self.default_branch}",
            f"  review: {review}",
            f"  approvals required: {self.required_approvals}",
            f"  required checks: {', '.join(self.required_checks) or 'none'}",
            f"  deploy: {self.workflow_path}{paths}, environment {self.environment}",
            f"  acceptance criteria: {len(self.criteria)}",
        ] + [f"    {c.id}: {c.text}" for c in self.criteria]


def run(cfg: RunnerConfig, issue: dict, flow: GitHubFlow) -> Preflight:
    settings = cfg.deploy_settings
    problems: list[str] = []
    try:
        repo = flow.repository()
    except GithubError as e:
        raise PreflightError([f"cannot read {flow.repo}: {e}"]) from e
    default = repo.get("default_branch") or "main"
    if not (repo.get("permissions") or {}).get("push"):
        problems.append(f"no write access to {flow.repo}; the runner must push and merge")

    merge_method = _merge_method(repo, settings.merge_method, problems)
    review_source, review_on_push, approvals, checks = _review_policy(flow, default, problems)

    workflow_name = PurePosixPath(settings.workflow).name
    trigger = PushTrigger()
    workflow_path = f".github/workflows/{workflow_name}"
    workflow = flow.workflow(workflow_name)
    if workflow is None:
        problems.append(f"deploy workflow {workflow_name} not found in {flow.repo}")
    else:
        workflow_path = workflow.get("path") or workflow_path
        if workflow.get("state") != "active":
            problems.append(f"deploy workflow {workflow_name} is {workflow.get('state')}")
        text = flow.file_text(workflow_path, default)
        try:
            found = push_trigger(text or "")
        except ValueError as e:
            problems.append(f"cannot read the triggers of {workflow_path}: {e}")
        else:
            if found is None:
                problems.append(f"{workflow_path} does not run on push, so a merge cannot deploy")
            elif not found.runs_on(default):
                problems.append(f"{workflow_path} does not run on a push to {default}")
            else:
                trigger = found

    if flow.environment(settings.environment) is None:
        problems.append(f"environment {settings.environment!r} not found in {flow.repo}")

    found_criteria = criteria_mod.parse(issue.get("body"))
    if not found_criteria:
        problems.append(
            f"issue #{issue.get('number')} has no acceptance criteria section; "
            "the Definition of Done cannot be met"
        )

    if problems:
        raise PreflightError(problems)
    return Preflight(
        repo=flow.repo,
        default_branch=default,
        merge_method=merge_method,
        review_source=review_source,
        review_on_push=review_on_push,
        required_approvals=approvals,
        required_checks=checks,
        workflow_path=workflow_path,
        trigger=trigger,
        environment=settings.environment,
        criteria=found_criteria,
    )


def _merge_method(repo: dict, configured: str, problems: list[str]) -> str:
    allowed = [name for name, flag in _MERGE_FLAGS if repo.get(flag)]
    if not allowed:
        problems.append("the repository allows no merge method")
        return ""
    if configured == "auto":
        if len(allowed) > 1:
            problems.append(
                f"the repository allows {', '.join(allowed)} merges; "
                "set [deploy] merge_method to the one to use"
            )
            return ""
        return allowed[0]
    if configured not in allowed:
        problems.append(
            f"[deploy] merge_method {configured} is not allowed here (allowed: {', '.join(allowed)})"
        )
        return ""
    return configured


def _review_policy(flow: GitHubFlow, branch: str, problems: list[str]):
    source, on_push, approvals, checks = "request", False, 0, []
    try:
        rules = flow.branch_rules(branch)
    except GithubError as e:
        problems.append(f"cannot read the rules for {branch}: {e}")
        rules = []
    for rule in rules:
        params = rule.get("parameters") or {}
        if rule.get("type") == "copilot_code_review":
            source = "ruleset"
            on_push = bool(params.get("review_on_push"))
        elif rule.get("type") == "pull_request":
            approvals = max(approvals, int(params.get("required_approving_review_count") or 0))
        elif rule.get("type") == "required_status_checks":
            checks += [c.get("context") for c in params.get("required_status_checks") or []]
    try:
        protection = flow.branch_protection(branch)
    except GithubError:
        protection = None  # only admins can read classic protection; rules cover the rest
    if protection:
        reviews = protection.get("required_pull_request_reviews") or {}
        approvals = max(approvals, int(reviews.get("required_approving_review_count") or 0))
        checks += (protection.get("required_status_checks") or {}).get("contexts") or []
    return source, on_push, approvals, sorted({c for c in checks if c})


# --- the `on:` block of a workflow file ---------------------------------------
# A deliberately small reader for the shapes GitHub workflows use for triggers.
# The runner has no third-party dependencies, and only `on.push` matters here.

_ON_KEY = re.compile(r"""^(["']?)(on|true)\1\s*:\s*(.*)$""")
_KEY = re.compile(r"""^(\s*)(["']?)([\w-]+)\2\s*:\s*(.*)$""")


def push_trigger(text: str) -> PushTrigger | None:
    """The push trigger of a workflow, or None when it does not run on push."""
    lines = [_strip_comment(line) for line in text.splitlines()]
    start = next((i for i, line in enumerate(lines) if _ON_KEY.match(line)), None)
    if start is None:
        raise ValueError("no top-level 'on:' key")
    inline = _ON_KEY.match(lines[start]).group(3).strip()
    if inline:
        events = _flow_list(inline) if inline.startswith("[") else [_scalar(inline)]
        return PushTrigger() if "push" in events else None
    block = _block(lines, start, 0)
    push = _find_key(block, "push")
    if push is None:
        return None
    index, value = push
    if value and value not in ("null", "~", "{}"):
        raise ValueError("an inline push mapping is not supported; use block style")
    trigger = PushTrigger()
    indent = _indent(block[index])
    body = _block(block, index, indent)
    for key, attr in (
        ("branches", "branches"),
        ("branches-ignore", "branches_ignore"),
        ("paths", "paths"),
        ("paths-ignore", "paths_ignore"),
    ):
        found = _find_key(body, key)
        if found is not None:
            setattr(trigger, attr, _list_value(body, *found))
    return trigger


def _strip_comment(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    quote = None
    for i, ch in enumerate(line):
        if ch in "\"'" and quote in (None, ch):
            quote = None if quote else ch
        elif ch == "#" and quote is None and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], start: int, parent_indent: int) -> list[str]:
    """The lines nested under lines[start], i.e. indented deeper than its key."""
    out = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent(line) <= parent_indent:
            break
        out.append(line)
    return out


def _find_key(block: list[str], key: str):
    levels = [_indent(line) for line in block if line.strip()]
    if not levels:
        return None
    level = min(levels)
    for i, line in enumerate(block):
        match = _KEY.match(line)
        if match and _indent(line) == level and match.group(3) == key:
            return i, match.group(4).strip()
    return None


def _list_value(block: list[str], index: int, inline: str) -> list[str]:
    if inline:
        return _flow_list(inline) if inline.startswith("[") else [_scalar(inline)]
    items = []
    for line in _block(block, index, _indent(block[index])):
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(_scalar(stripped[2:]))
        elif stripped:
            raise ValueError(f"unexpected line in a list: {stripped!r}")
    return items


def _flow_list(text: str) -> list[str]:
    if not (text.startswith("[") and text.endswith("]")):
        raise ValueError(f"unsupported value {text!r}")
    return [_scalar(part) for part in text[1:-1].split(",") if part.strip()]


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _glob(value: str, pattern: str) -> bool:
    # GitHub's `**` matches across slashes; fnmatch's `*` already does
    return fnmatch.fnmatchcase(value, pattern.replace("**", "*"))
