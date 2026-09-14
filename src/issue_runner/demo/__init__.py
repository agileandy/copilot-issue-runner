"""Self-contained offline demo: a sandbox repo plus a scripted stand-in for copilot.

`--demo` exists so the pipeline can be shown end to end — plan, red test, green
code, verifier hand-back, per-ticket commit — with no model call, no credit
spend and no GitHub access. Everything is generated on disk from the constants
below rather than shipped as package data, so the demo works identically from a
wheel, a checkout or `uv run`.

The stand-in binary is `responder.py`, invoked through a tiny shell shim because
the runner takes a single command string, not an argv list.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ISSUE_MD = """\
# Add mean and median to the stats module

`demo_pkg.stats` is empty. The demo needs two summary statistics:

- `mean(values)` returning the arithmetic mean
- `median(values)` returning the middle value, handling even-length input

Both must be covered by tests.
"""

CONFTEST_PY = """\
# Root conftest keeps the repository importable for the tests below it.
"""

PYPROJECT_TOML = """\
[project]
name = "demo-stats"
version = "0.0.1"
description = "issue-runner demo sandbox"
requires-python = ">=3.12"
"""

README_MD = """\
# issue-runner demo sandbox

A throwaway git repository created by `gh-runner --demo`. The runner plans the
issue in `issue.md`, then drives a scripted stand-in for Copilot through the
TDD loop, committing one ticket at a time.

Inspect the result with `git log --oneline` and `git show`.
"""

PACKAGE_INIT = '"""Demo package the runner implements against."""\n'

STATS_PY = '''\
"""Summary statistics. Deliberately empty: the runner fills this in."""
'''

# the demo's own test command: this interpreter, so the sandbox needs no venv
PYTEST_CMD_TEMPLATE = "{python} -m pytest {{test_path}} -q"
# fallback for an interpreter without pytest (e.g. a `uv tool install` venv)
MINITEST_CMD_TEMPLATE = "{python} {minitest} {{test_path}}"


@dataclass
class DemoEnv:
    repo_dir: Path
    issue_file: Path
    copilot_cmd: Path
    test_cmd: str
    temporary: bool


class DemoError(RuntimeError):
    pass


def _git(repo_dir: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise DemoError(f"git {' '.join(args)} failed in the demo sandbox: {result.stderr.strip()}")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_shim(state_dir: Path) -> Path:
    """A shell wrapper so the scripted responder can be used as `copilot_cmd`."""
    shim = state_dir / "demo-copilot"
    responder = Path(__file__).with_name("responder.py")
    shim.write_text(f'#!/bin/sh\nexec {_quote(sys.executable)} {_quote(str(responder))} "$@"\n')
    shim.chmod(0o755)
    return shim


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def demo_test_cmd() -> str:
    """pytest when this interpreter has it, otherwise the bundled mini runner."""
    if importlib.util.find_spec("pytest") is not None:
        return PYTEST_CMD_TEMPLATE.format(python=_quote(sys.executable))
    minitest = Path(__file__).with_name("minitest.py")
    return MINITEST_CMD_TEMPLATE.format(
        python=_quote(sys.executable), minitest=_quote(str(minitest))
    )


def setup_demo(dest: Path | None = None, force: bool = False) -> DemoEnv:
    """Create (or recreate) the demo sandbox and return everything the CLI needs."""
    if shutil.which("git") is None:
        raise DemoError("the demo needs git on PATH")
    temporary = dest is None
    repo_dir = (
        Path(tempfile.mkdtemp(prefix="issue-runner-demo-")) if temporary else Path(dest).resolve()
    )
    if not temporary and repo_dir.exists():
        if not force and any(repo_dir.iterdir()):
            raise DemoError(
                f"{repo_dir} is not empty — pass --demo-reset to recreate it, or choose "
                "another --demo-dir"
            )
        if force:
            shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    _write(repo_dir / "issue.md", ISSUE_MD)
    _write(repo_dir / "README.md", README_MD)
    _write(repo_dir / "pyproject.toml", PYPROJECT_TOML)
    _write(repo_dir / "conftest.py", CONFTEST_PY)
    _write(repo_dir / "demo_pkg" / "__init__.py", PACKAGE_INIT)
    _write(repo_dir / "demo_pkg" / "stats.py", STATS_PY)
    _write(repo_dir / "tests" / ".gitkeep", "")

    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "demo@issue-runner.local")
    _git(repo_dir, "config", "user.name", "issue-runner demo")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "chore: demo sandbox skeleton")

    state_dir = repo_dir / ".issue-runner"
    state_dir.mkdir(exist_ok=True)
    shim = _write_shim(state_dir)

    return DemoEnv(
        repo_dir=repo_dir,
        issue_file=repo_dir / "issue.md",
        copilot_cmd=shim,
        test_cmd=demo_test_cmd(),
        temporary=temporary,
    )


def demo_banner(env: DemoEnv) -> str:
    lines = [
        "demo mode — no model calls, no credits, no GitHub access",
        f"  sandbox repo : {env.repo_dir}",
        f"  issue        : {env.issue_file}",
        f"  copilot stand-in: {env.copilot_cmd}",
        f"  test command : {env.test_cmd}",
    ]
    if env.temporary:
        lines.append("  (temporary directory — pass --demo-dir PATH to keep it somewhere)")
    delay = os.environ.get("ISSUE_RUNNER_DEMO_DELAY")
    if delay:
        lines.append(f"  pacing       : {delay}s per scripted call")
    return "\n".join(lines)
