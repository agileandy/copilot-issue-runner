"""Give each run worktree its own toolchain before any agent starts.

A fresh `git worktree add` has no `.venv` and no `node_modules`. Agents then
reached for the primary checkout's environment, which lies outside the
directory Copilot may touch, and every such call was denied. Provisioning the
worktree itself keeps every test command inside it.

The steps are discovered from the target repository, never hardcoded:

| Marker | Steps |
|---|---|
| `uv.lock` at the root | `uv sync --frozen` |
| `requirements*.txt` at the root or one level down | `uv venv`, then one `uv pip install -r` per file |
| `package-lock.json` up to two levels down | `npm ci` in that directory |

Requirement files are installed one at a time, `requirements.txt` first, so a
later file may re-pin a package an earlier one pinned (as `pip install -r a`
then `-r b` would) instead of failing as a conflict. When the test command
runs pytest and no requirement file names it, pytest is added.

Every step uses uv's or npm's shared cache, is skipped once its marker shows a
finished install, and is bounded by `provision_timeout`.

`setup_cmd` replaces discovery with the repository's own commands, for steps
no manifest describes (`run_setup`).
"""

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("issue_runner")

STAMP = ".issue-runner-provisioned"
# directories never searched for manifests: installed dependencies and build output
_SKIP_DIRS = {"node_modules", "cdk.out", "dist", "build", "coverage"}
_PYTEST_RE = re.compile(r"^\s*pytest\b", re.IGNORECASE | re.MULTILINE)
# kept out of every commit, in every worktree of the repository
EXCLUDES = ("/.venv/", "node_modules/")


class ProvisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    label: str
    cwd: str  # relative to the worktree root; "." is the root
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    kind: str  # "python" or "node"
    directory: str  # relative to the worktree root
    steps: tuple[Step, ...]


def venv_python(root: Path) -> Path:
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return Path(root) / ".venv" / relative


def _visible_dirs(root: Path, depth: int) -> list[Path]:
    found = [root]
    frontier = [root]
    for _ in range(depth):
        next_frontier = []
        for directory in frontier:
            try:
                children = sorted(p for p in directory.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.name in _SKIP_DIRS:
                    continue
                next_frontier.append(child)
        found += next_frontier
        frontier = next_frontier
    return found


def _requirement_files(root: Path) -> list[Path]:
    files = []
    for directory in _visible_dirs(root, 1):
        named = sorted(directory.glob("requirements*.txt"))
        # the runtime set first, so a dev file can re-pin what it needs
        named.sort(key=lambda p: (p.name != "requirements.txt", p.name))
        files += [p for p in named if p.is_file()]
    return files


def _needs_pytest(commands: list[str], requirement_files: list[Path]) -> bool:
    if not any("pytest" in c for c in commands if c):
        return False
    for path in requirement_files:
        try:
            if _PYTEST_RE.search(path.read_text()):
                return False
        except OSError:
            continue
    return True


def _python_plan(root: Path, commands: list[str]) -> Plan | None:
    if (root / "uv.lock").is_file() and (root / "pyproject.toml").is_file():
        return Plan("python", ".", (Step("uv sync", ".", ("uv", "sync", "--frozen")),))
    requirements = _requirement_files(root)
    if not requirements:
        return None
    python = str(venv_python(Path(".")))
    steps = [Step("uv venv", ".", ("uv", "venv", "--allow-existing", ".venv"))]
    for path in requirements:
        relative = path.relative_to(root).as_posix()
        steps.append(
            Step(
                f"uv pip install -r {relative}",
                ".",
                ("uv", "pip", "install", "--python", python, "-r", relative),
            )
        )
    if _needs_pytest(commands, requirements):
        steps.append(
            Step(
                "uv pip install pytest", ".", ("uv", "pip", "install", "--python", python, "pytest")
            )
        )
    return Plan("python", ".", tuple(steps))


def _node_plans(root: Path) -> list[Plan]:
    plans = []
    for directory in _visible_dirs(root, 2):
        if not (directory / "package-lock.json").is_file():
            continue
        relative = directory.relative_to(root).as_posix() or "."
        argv = ("npm", "ci", "--no-audit", "--no-fund", "--prefer-offline")
        plans.append(Plan("node", relative, (Step(f"npm ci in {relative}", relative, argv),)))
    return plans


def discover(root: Path, commands: list[str] | None = None) -> list[Plan]:
    """What this worktree needs, from its own manifests."""
    root = Path(root)
    plans = []
    python = _python_plan(root, [c for c in (commands or []) if c])
    if python:
        plans.append(python)
    plans += _node_plans(root)
    return plans


def is_provisioned(root: Path, plan: Plan) -> bool:
    if plan.kind == "python":
        return venv_python(root).is_file() and (root / ".venv" / STAMP).is_file()
    # npm writes this hidden lockfile only once an install has completed
    return (root / plan.directory / "node_modules" / ".package-lock.json").is_file()


def _mark_provisioned(root: Path, plan: Plan) -> None:
    if plan.kind == "python" and (root / ".venv").is_dir():
        (root / ".venv" / STAMP).write_text("provisioned by issue-runner\n")


def provision(root: Path, commands: list[str], timeout: int, run=subprocess.run) -> list[str]:
    """Run every missing step in the worktree. Returns the labels that ran.

    A failed or timed-out step raises ProvisionError: a run whose tests cannot
    start would only spend model calls discovering that.
    """
    root = Path(root)
    ran: list[str] = []
    plans = discover(root, commands)
    if not plans:
        log.info("toolchain: no lockfile or requirements found in %s; nothing to provision", root)
        return ran
    for plan in plans:
        where = root / plan.directory
        if is_provisioned(root, plan):
            log.info("toolchain: %s in %s already provisioned; skipped", plan.kind, where)
            continue
        for step in plan.steps:
            log.info("toolchain: %s (in %s, timeout %ss)", step.label, root / step.cwd, timeout)
            try:
                result = run(
                    list(step.argv),
                    cwd=str(root / step.cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    env=dict(os.environ, CI="1", NO_COLOR="1"),
                )
            except subprocess.TimeoutExpired as e:
                raise ProvisionError(
                    f"toolchain step `{step.label}` timed out after {timeout}s in {root / step.cwd}; "
                    "raise provision_timeout or set provision = false and prepare it yourself"
                ) from e
            except OSError as e:
                raise ProvisionError(f"toolchain step `{step.label}` could not start: {e}") from e
            if result.returncode != 0:
                detail = ((result.stderr or "") + (result.stdout or "")).strip()[-1500:]
                raise ProvisionError(
                    f"toolchain step `{step.label}` failed (exit {result.returncode}) "
                    f"in {root / step.cwd}:\n{detail}"
                )
            ran.append(step.label)
        _mark_provisioned(root, plan)
    return ran


def run_setup(root: Path, commands: list[str], timeout: int, run=subprocess.run) -> list[str]:
    """Run the repository's own `setup_cmd` commands in the worktree, in order."""
    root = Path(root)
    env = dict(os.environ, CI="1", NO_COLOR="1")
    # an activated venv in the caller's shell must not receive the install
    env.pop("VIRTUAL_ENV", None)
    for command in commands:
        log.info("toolchain: setup_cmd `%s` (in %s, timeout %ss)", command, root, timeout)
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ProvisionError(f"setup_cmd `{command}` could not be parsed: {e}") from e
        try:
            result = run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise ProvisionError(
                f"setup_cmd `{command}` timed out after {timeout}s in {root}; "
                "raise provision_timeout"
            ) from e
        except OSError as e:
            raise ProvisionError(f"setup_cmd `{command}` could not start: {e}") from e
        if result.returncode != 0:
            detail = ((result.stderr or "") + (result.stdout or "")).strip()[-1500:]
            raise ProvisionError(
                f"setup_cmd `{command}` failed (exit {result.returncode}) in {root}:\n{detail}"
            )
    return list(commands)
