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
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
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

STATE_DIR_NAME = ".issue-runner"
MARKER_NAME = "demo-sandbox.json"
# unambiguous: only setup_demo() ever writes this string
MARKER_MAGIC = "issue-runner demo sandbox — safe to delete"

# absolute directories a demo sandbox must never be, even with a forged marker
DANGEROUS_DIRS = frozenset(
    {
        "/",
        "/Applications",
        "/Library",
        "/System",
        "/Users",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
        "/var/tmp",
    }
)


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


def _exclude_state_dir(repo_dir: Path) -> None:
    """Keep the ownership marker out of the index so clean-tree checks still hold."""
    exclude = repo_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.is_file() else ""
    pattern = f"/{STATE_DIR_NAME}/"
    if pattern not in existing.splitlines():
        exclude.write_text(existing.rstrip("\n") + f"\n{pattern}\n")


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


def _resolve_or_refuse(label: str, produce) -> Path:
    """Resolve a path the guards depend on, or refuse to continue.

    Fail closed: if we cannot work out where home, the cwd, the project checkout
    or the destination actually is, we cannot prove a reset is safe, so nothing
    is deleted.
    """
    try:
        return Path(produce()).resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as exc:
        raise DemoError(
            f"cannot resolve {label} ({exc.__class__.__name__}: {exc}), so the demo "
            "cannot prove the sandbox path is safe to touch. Run from a directory "
            "that exists and is readable, or pass --demo-dir with an absolute "
            "throwaway path."
        ) from exc


def _project_root() -> Path:
    """The checkout (or install tree) this package lives in — never a demo sandbox."""
    here = _resolve_or_refuse("the issue-runner install directory", lambda: Path(__file__))
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() or (parent / ".git").exists():
            return parent
    return here.parents[2]


def _protected_dirs() -> list[Path]:
    dirs = [Path(path) for path in DANGEROUS_DIRS]
    dirs.append(_resolve_or_refuse("the home directory", Path.home))
    dirs.append(_resolve_or_refuse("the current working directory", Path.cwd))
    dirs.append(_resolve_or_refuse("the project root", _project_root))
    return dirs


def _reject_dangerous_dest(raw: Path, repo_dir: Path) -> None:
    """Refuse anything that is not plausibly a throwaway directory.

    Runs before any ownership check and before any deletion, so a forged marker
    on `/` or `$HOME` still cannot reach `shutil.rmtree`.
    """
    try:
        is_link = raw.is_symlink()
    except OSError as exc:
        raise DemoError(f"cannot inspect {raw} ({exc}) — refusing to use it as the demo") from exc
    if is_link:
        raise DemoError(f"{raw} is a symlink — refusing to use it as the demo sandbox")
    if repo_dir.parent == repo_dir:
        raise DemoError(f"{repo_dir} is a filesystem root — refusing to use it as the demo sandbox")
    for protected in _protected_dirs():
        if repo_dir == protected:
            raise DemoError(
                f"{repo_dir} is a protected directory — refusing to use it as the demo "
                "sandbox; pass --demo-dir with a throwaway path"
            )
        if repo_dir in protected.parents:
            raise DemoError(
                f"{repo_dir} contains {protected} — refusing to use it as the demo "
                "sandbox; pass --demo-dir with a throwaway path"
            )


def _marker_path(repo_dir: Path) -> Path:
    return repo_dir / STATE_DIR_NAME / MARKER_NAME


def _write_marker(repo_dir: Path) -> Path:
    marker = _marker_path(repo_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "marker": MARKER_MAGIC,
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
                "created_by": "issue_runner.demo.setup_demo",
            },
            indent=2,
        )
        + "\n"
    )
    return marker


def _is_owned_demo_dir(repo_dir: Path) -> bool:
    state_dir = repo_dir / STATE_DIR_NAME
    marker = _marker_path(repo_dir)
    if state_dir.is_symlink() or not state_dir.is_dir():
        return False
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("marker") == MARKER_MAGIC


def _require_demo_ownership(repo_dir: Path) -> None:
    if _is_owned_demo_dir(repo_dir):
        return
    raise DemoError(
        f"{repo_dir} was not created by the demo (no {STATE_DIR_NAME}/{MARKER_NAME} "
        "ownership marker), so --demo-reset refuses to delete it. Move or remove it "
        "yourself, or pass --demo-dir with an empty or throwaway path."
    )


def setup_demo(dest: Path | None = None, force: bool = False) -> DemoEnv:
    """Create (or recreate) the demo sandbox and return everything the CLI needs."""
    if shutil.which("git") is None:
        raise DemoError("the demo needs git on PATH")
    temporary = dest is None
    if temporary:
        repo_dir = Path(tempfile.mkdtemp(prefix="issue-runner-demo-"))
    else:
        raw = Path(dest)
        repo_dir = _resolve_or_refuse(f"the demo destination {raw}", lambda: raw)
        _reject_dangerous_dest(raw, repo_dir)
        if repo_dir.exists():
            if not repo_dir.is_dir():
                raise DemoError(f"{repo_dir} is not a directory — choose another --demo-dir")
            if any(repo_dir.iterdir()):
                if not force:
                    raise DemoError(
                        f"{repo_dir} is not empty — pass --demo-reset to recreate it, or choose "
                        "another --demo-dir"
                    )
                _require_demo_ownership(repo_dir)
                shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_marker(repo_dir)

    _write(repo_dir / "issue.md", ISSUE_MD)
    _write(repo_dir / "README.md", README_MD)
    _write(repo_dir / "pyproject.toml", PYPROJECT_TOML)
    _write(repo_dir / "conftest.py", CONFTEST_PY)
    _write(repo_dir / "demo_pkg" / "__init__.py", PACKAGE_INIT)
    _write(repo_dir / "demo_pkg" / "stats.py", STATS_PY)
    _write(repo_dir / "tests" / ".gitkeep", "")

    _git(repo_dir, "init", "-b", "main")
    _exclude_state_dir(repo_dir)
    _git(repo_dir, "config", "user.email", "demo@issue-runner.local")
    _git(repo_dir, "config", "user.name", "issue-runner demo")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "chore: demo sandbox skeleton")

    state_dir = repo_dir / STATE_DIR_NAME
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
