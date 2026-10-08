"""Merge: bring the base branch into the run branch when GitHub says the PR is
behind or conflicted, so the merge that follows is of reviewed, tested code.

The runner merges (never rebases or force-pushes) in the run worktree without
committing, then holds the result to the same checks as any other change:

- no unmerged path and no conflict marker left, as git itself reports them,
- frozen ticket tests untouched, unless the conflict was in that test file,
- Dev checks untouched,
- every ticket test and the regression suite green.

When the merge conflicts or a check fails, the `resolver` agent gets the
conflicted paths and the failure, within the coder's retry budget. If it
cannot produce a passing result the runner aborts its own merge, which leaves
the run branch exactly as it was.
"""

import base64
import hashlib

from ..agent_rules import workspace_rules
from ..config import RunnerConfig
from ..tickets import TicketStore
from . import devops
from .build import resolve_test_path
from .review import ReviewError, green_problem, require_frozen_checks


class MergeError(RuntimeError):
    pass


RESOLVER_PROMPT = """\
You are the resolver in an automated delivery pipeline working on this repository.

The branch for GitHub issue #{number} ({title}) is being brought up to date with
`{base}`. A merge of `origin/{base}` is IN PROGRESS in the working tree.

{situation}

Edit the working tree so that the merge keeps BOTH sides' intent: the issue's
change and what landed on `{base}`. Remove every conflict marker.

HARD RULES:
- Do NOT run git commands that change state (no add, commit, merge, rebase,
  reset, checkout or stash). The runner finishes the merge.
- Do not change these accepted test files unless they are conflicted:
{tests}
- Keep the change minimal.

{rules}
When done, reply with ONLY this JSON: {{"notes": "<one line>"}}
{feedback}"""


RESOLVER_PR_PROMPT = """\
You are the resolver in an automated delivery pipeline working on this repository.

The pull request branch `{branch}` is being brought up to date with `{base}`.
A merge of `origin/{base}` is IN PROGRESS in the working tree.

These paths conflict:
{conflicted}

What is wrong now:
{problem}

Edit the working tree so that the merge keeps BOTH sides' intent: the branch's
change and what landed on `{base}`. Remove every conflict marker.

HARD RULES:
- Do NOT run git commands that change state (no add, commit, merge, rebase,
  reset, checkout or stash). The runner finishes the merge.
- Keep the change minimal.

{rules}
When done, reply with ONLY this JSON: {{"notes": "<one line>"}}
{feedback}"""


def update_branch(client, cfg: RunnerConfig, issue: dict, store: TicketStore, base: str) -> str:
    """Merge origin/<base> into the run branch, resolving if needed; return the new head."""
    repo = cfg.repo_dir
    head = devops.head_commit(repo)
    devops.fetch(repo)
    conflicted = devops.merge_in(
        repo, f"origin/{base}", f"chore: merge origin/{base} into {store.branch}"
    )
    try:
        tests = [t for t in store.tickets if t.test_hash and t.test_path]
        problem = _problem(cfg, store, tests, conflicted)
        feedback = ""
        attempts = 0
        while problem is not None:
            if attempts > cfg.coder_retries:
                devops.abort_merge(repo)
                raise MergeError(f"could not merge origin/{base}: {problem}")
            attempts += 1
            situation = (
                "These paths conflict:\n" + "\n".join(f"  - {p}" for p in conflicted)
                if conflicted
                else "The merge applied cleanly, but the result fails the checks."
            )
            client.run(
                RESOLVER_PROMPT.format(
                    number=issue["number"],
                    title=issue["title"],
                    base=base,
                    situation=f"{situation}\n\nWhat is wrong now:\n{problem}",
                    tests="\n".join(f"  - {t.test_path}" for t in tests) or "  (none)",
                    rules=workspace_rules(cfg),
                    feedback=feedback,
                ),
                role="resolver",
                session_name=f"resolver-{store.issue_ref}",
            )
            if devops.current_branch(repo) != store.branch or devops.head_commit(repo) != head:
                devops.abort_merge(repo)
                raise MergeError("the resolver moved HEAD or the branch; only the runner commits")
            if not devops.merge_in_progress(repo):
                raise MergeError("the resolver ended the merge; only the runner finishes it")
            problem = _problem(cfg, store, tests, conflicted)
            feedback = f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED (fix this):\n{problem}" if problem else ""
        sha = devops.finish_merge(repo, store.branch)
    except BaseException:
        devops.abort_merge(repo)
        raise
    _refreeze_merged_tests(cfg, store, tests, conflicted)
    store.last_commit = sha
    store.save()
    return sha


def update_pr_branch(client, cfg: RunnerConfig, branch: str, base: str) -> str:
    """Merge origin/<base> into `branch` (already checked out) and push it fast-forward."""
    repo = cfg.repo_dir
    head = devops.head_commit(repo)
    devops.fetch(repo)
    if devops.is_ancestor(repo, f"origin/{base}", "HEAD"):
        return devops.head_commit(repo)
    conflicted = devops.merge_in(
        repo, f"origin/{base}", f"chore: merge origin/{base} into {branch}"
    )
    snapshot = devops.merge_snapshot(repo)
    try:
        problem = _pr_problem(cfg, conflicted)
        feedback = ""
        attempts = 0
        while problem is not None or (conflicted and attempts == 0):
            if not conflicted or attempts > cfg.coder_retries:
                devops.abort_merge(repo)
                raise MergeError(f"could not merge origin/{base}: {problem}")
            attempts += 1
            client.run(
                RESOLVER_PR_PROMPT.format(
                    branch=branch,
                    base=base,
                    conflicted="\n".join(f"  - {p}" for p in conflicted),
                    problem=problem or _UNMARKED_CONFLICT,
                    rules=workspace_rules(cfg),
                    feedback=feedback,
                ),
                role="resolver",
                session_name=f"resolver-pr-{branch}",
            )
            if devops.current_branch(repo) != branch or devops.head_commit(repo) != head:
                devops.abort_merge(repo)
                raise MergeError("the resolver moved HEAD or the branch; only the runner commits")
            if not devops.merge_in_progress(repo):
                raise MergeError("the resolver ended the merge; only the runner finishes it")
            stray = [p for p in devops.changed_since(repo, snapshot) if p not in conflicted]
            if stray:
                problem = f"the resolver edited files outside the conflict: {', '.join(stray)}"
            else:
                problem = _pr_problem(cfg, conflicted)
            feedback = f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED (fix this):\n{problem}" if problem else ""
        sha = devops.finish_merge(repo, branch)
    except BaseException:
        devops.abort_merge(repo)
        raise
    try:
        devops.push_branch(repo, branch)
    except devops.DevopsError as e:
        devops.reset_hard(repo, head)
        raise MergeError(
            f"origin/{branch} moved during the update; nothing was pushed, rerun --update-pr"
        ) from e
    return sha


_UNMARKED_CONFLICT = (
    "git left these paths unmerged without conflict markers (a modify/delete, rename or "
    "binary conflict); decide what each should hold so both sides' intent is kept"
)


def _pr_problem(cfg, conflicted: list[str]) -> str | None:
    markers = devops.leftover_markers(cfg.repo_dir, conflicted)
    if markers:
        return "conflict markers remain:\n" + "\n".join(markers[:20])
    return green_problem(cfg, [])


def _problem(cfg, store, tests, conflicted: list[str]) -> str | None:
    repo = cfg.repo_dir
    markers = devops.leftover_markers(repo, conflicted)
    if markers:
        return "conflict markers remain:\n" + "\n".join(markers[:20])
    changed_tests = [
        t.test_path
        for t in tests
        if t.test_path not in conflicted and _hash(cfg, t.test_path) != t.test_hash
    ]
    if changed_tests:
        for t in tests:
            if t.test_path in changed_tests:
                resolve_test_path(cfg, t.test_path).write_bytes(base64.b64decode(t.test_snapshot))
        return f"accepted test files were changed and restored: {', '.join(changed_tests)}"
    try:
        require_frozen_checks(store)
    except ReviewError as e:
        raise MergeError(str(e)) from e
    return green_problem(cfg, tests)


def _hash(cfg, path: str) -> str | None:
    full = resolve_test_path(cfg, path)
    return hashlib.sha256(full.read_bytes()).hexdigest() if full.is_file() else None


def _refreeze_merged_tests(cfg, store, tests, conflicted: list[str]) -> None:
    """A test file that conflicted now holds both sides; that merged file is the spec."""
    for ticket in tests:
        if ticket.test_path in conflicted:
            content = resolve_test_path(cfg, ticket.test_path).read_bytes()
            ticket.test_snapshot = base64.b64encode(content).decode("ascii")
            ticket.test_hash = hashlib.sha256(content).hexdigest()
