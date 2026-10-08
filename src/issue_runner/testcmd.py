"""Detect the target repository's test command from its project markers.

A "generic" runner that always shelled out to pytest was a footgun: on a Node,
Go or Rust repo every tester attempt produced a test the harness could never
turn green, so every ticket blocked. Detection only supplies a *default* —
`runner.toml`'s `test_cmd` and `--test-cmd` still win, unchanged.

Commands are chosen so a failing test genuinely fails, and so the runner prints
evidence that individual cases ran. Notably `go test` and `cargo test` are run
without a path filter: their selector flags take a test NAME, not a file path,
so filtering by path would match nothing and exit 0 — the harness would read
that as a passing test and the TDD loop would break.
"""

import ast
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEST_CMD = "python -m pytest {test_path} -q"
DEFAULT_REGRESSION_CMD = "python -m pytest -q"
# -v names every case (the only per-test evidence go prints) and -count=1
# defeats the result cache, which otherwise replays "ok pkg (cached)" without
# running anything.
GO_TEST_CMD = "go test -v -count=1 ./..."


@dataclass(frozen=True)
class Detection:
    test_cmd: str
    marker: str | None  # None = nothing recognised; test_cmd is the fallback


def _python_test_cmd(marker: Path) -> str:
    repo = marker.parent
    if (repo / "uv.lock").is_file():
        python = "uv run --no-sync python"
    else:
        relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        # relative to the run directory: an absolute path would point every run
        # worktree back at the source checkout's environment, outside the
        # directory the agent is allowed to touch
        project_python = Path(".venv") / relative
        python = (
            shlex.quote(project_python.as_posix())
            if (repo / project_python).is_file()
            else "python"
        )
    return f"{python} -m pytest {{test_path}} -q"


def _python_regression_cmd(marker: Path) -> str:
    return _python_test_cmd(marker).replace(" {test_path}", "")


def _node_test_cmd(path: Path) -> str | None:
    """Node counts as a marker only if package.json actually defines a test script."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and scripts.get("test"):
        return "npm test -- {test_path}"
    return None


def _node_regression_cmd(path: Path) -> str | None:
    return "npm test" if _node_test_cmd(path) else None


# ordered: the first marker that matches wins in a polyglot repo
_MARKERS: tuple[tuple[str, object], ...] = (
    ("pyproject.toml", _python_test_cmd),
    ("setup.py", _python_test_cmd),
    ("setup.cfg", _python_test_cmd),
    ("package.json", _node_test_cmd),
    ("go.mod", GO_TEST_CMD),
    ("Cargo.toml", "cargo test"),
)

# the same markers, but the whole suite: no {test_path} placeholder. `go test`
# and `cargo test` are already whole-suite commands.
_REGRESSION_MARKERS: tuple[tuple[str, object], ...] = (
    ("pyproject.toml", _python_regression_cmd),
    ("setup.py", _python_regression_cmd),
    ("setup.cfg", _python_regression_cmd),
    ("package.json", _node_regression_cmd),
    ("go.mod", GO_TEST_CMD),
    ("Cargo.toml", "cargo test"),
)


def _first_match(repo_dir: Path, markers) -> tuple[str, str] | None:
    for name, recipe in markers:
        path = Path(repo_dir) / name
        if not path.is_file():
            continue
        cmd = recipe(path) if callable(recipe) else recipe
        if cmd:
            return cmd, name
    return None


def detect_test_cmd(repo_dir: Path) -> Detection:
    found = _first_match(repo_dir, _MARKERS)
    return Detection(*found) if found else Detection(DEFAULT_TEST_CMD, None)


def detect_regression_cmd(repo_dir: Path) -> str | None:
    """Full-suite command for the recognised project, or None if unrecognised.

    Unlike `detect_test_cmd` there is no fallback: running pytest over an
    unidentified repository would be a guess, and a regression gate that guesses
    is worse than no gate.
    """
    found = _first_match(repo_dir, _REGRESSION_MARKERS)
    return found[0] if found else None


_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", ".issue-runner"})


def _module_names(changed_file: str) -> set[str]:
    parts = list(Path(changed_file).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    return {".".join(parts), parts[-1]}


def _imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def related_tests(repo_dir: Path, changed_files: list[str]) -> list[str]:
    """Repo-relative test files whose imports name a changed Python module."""
    modules: set[str] = set()
    for changed in changed_files:
        if changed.endswith(".py"):
            modules |= _module_names(changed)
    if not modules:
        return []
    repo = Path(repo_dir)
    related: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not _is_test_file(name):
                continue
            path = Path(root) / name
            imported = _imported_modules(path)
            if any(imp == mod or imp.startswith(mod + ".") for imp in imported for mod in modules):
                related.append(path.relative_to(repo).as_posix())
    return sorted(related)
