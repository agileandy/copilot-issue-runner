"""Prepare a run worktree's dependencies before any test runs in it.

A new git worktree has none of the source checkout's ignored directories, so it
starts without `.venv` or `node_modules`. Without setup, a focused test and the
full suite can end up in different environments, or fail on a missing import.

`setup_cmd` (runner.toml) or `--setup-cmd` always wins. Otherwise setup follows
the project marker the test command was detected from, and runs only when that
command was detected: an explicit `--test-cmd` already names its environment.
"""

import logging
import os
import shlex
import subprocess
import tomllib
from pathlib import Path

from .config import RunnerConfig
from .phases import devops
from .testcmd import detect_test_cmd

log = logging.getLogger("issue_runner")

_SETUP_TIMEOUT = 1800
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")
# frozen installs only: an install that rewrote the lock file would change the
# worktree and invalidate work a verifier already approved
_NODE_LOCKS = (
    ("package-lock.json", "npm ci"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn install --frozen-lockfile"),
    ("bun.lock", "bun install --frozen-lockfile"),
    ("bun.lockb", "bun install --frozen-lockfile"),
)


class SetupError(devops.DevopsError):
    pass


def _venv_python() -> str:
    return ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"


def _is_installable(repo: Path) -> bool:
    if (repo / "setup.py").is_file():
        return True
    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "project" in data


def _python_best_effort(repo: Path) -> list[str]:
    """No lock file: a worktree .venv with the project, root requirements and pytest."""
    install = ["uv", "pip", "install", "--python", _venv_python()]
    if _is_installable(repo):
        install += ["-e", "."]
    for requirements in sorted(repo.glob("requirements*.txt")):
        install += ["-r", requirements.name]
    install.append("pytest")
    return ["uv venv --allow-existing .venv", shlex.join(install)]


def detect_setup(repo: Path) -> tuple[list[str], str | None]:
    """Setup commands for the marker the test command is detected from."""
    marker = detect_test_cmd(repo).marker
    if marker in _PYTHON_MARKERS:
        if (repo / "uv.lock").is_file():
            return ["uv sync --frozen"], "uv.lock"
        return _python_best_effort(repo), f"{marker} without a lock file"
    if marker == "package.json":
        for lock, command in _NODE_LOCKS:
            if (repo / lock).is_file():
                return [command], lock
    return [], None


def prepare(cfg: RunnerConfig, workspace: Path) -> None:
    """Run the setup commands in `workspace`; raise SetupError if any fails."""
    if cfg.setup_cmd:
        commands, source = list(cfg.setup_cmd), "setup_cmd"
    elif cfg.test_cmd_detected:
        commands, source = detect_setup(workspace)
    else:
        return
    if not commands:
        log.info("no environment setup needed for the run worktree")
        return
    before_paths = devops.changed_paths(workspace)
    before = devops.workspace_digest(workspace)
    env = dict(os.environ)
    # an activated venv in the caller's shell must not receive the install
    env.pop("VIRTUAL_ENV", None)
    for command in commands:
        cfg.control.check()
        log.info("setting up the run environment (%s): %s", source, command)
        try:
            argv = shlex.split(command)
            result = subprocess.run(
                argv,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=_SETUP_TIMEOUT,
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except ValueError as e:
            raise SetupError(
                f"environment setup command {command!r} could not be parsed: {e}"
            ) from e
        except FileNotFoundError:
            raise SetupError(
                f"environment setup could not run `{command}`: {argv[0]} is not installed"
            ) from None
        except subprocess.TimeoutExpired:
            raise SetupError(
                f"environment setup timed out after {_SETUP_TIMEOUT}s: `{command}`"
            ) from None
        if result.returncode:
            output = (result.stdout + result.stderr).strip()[-2000:]
            raise SetupError(
                f"environment setup failed in {workspace}: `{command}` exited "
                f"{result.returncode}; set setup_cmd in runner.toml or pass --setup-cmd "
                f"to prepare this project's dependencies\n{output}"
            )
    if devops.workspace_digest(workspace) != before:
        changed = sorted(set(devops.changed_paths(workspace)) ^ set(before_paths))
        named = ", ".join(changed[:10]) or "the contents of already-changed files"
        raise SetupError(
            f"environment setup changed files in the run worktree: {named}; "
            "setup must only write ignored paths"
        )
