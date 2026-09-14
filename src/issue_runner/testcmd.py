"""Detect the target repository's test command from its project markers.

A "generic" runner that always shelled out to pytest was a footgun: on a Node,
Go or Rust repo every tester attempt produced a test the harness could never
turn green, so every ticket blocked. Detection only supplies a *default* —
`runner.toml`'s `test_cmd` and `--test-cmd` still win, unchanged.

Commands are chosen so a failing test genuinely fails. Notably `go test` and
`cargo test` are run without a path filter: their selector flags take a test
NAME, not a file path, so filtering by path would match nothing and exit 0 —
the harness would read that as a passing test and the TDD loop would break.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEST_CMD = f"{sys.executable} -m pytest {{test_path}} -q"
DEFAULT_REGRESSION_CMD = f"{sys.executable} -m pytest -q"


@dataclass(frozen=True)
class Detection:
    test_cmd: str
    marker: str | None  # None = nothing recognised; test_cmd is the fallback


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
    ("pyproject.toml", DEFAULT_TEST_CMD),
    ("setup.py", DEFAULT_TEST_CMD),
    ("setup.cfg", DEFAULT_TEST_CMD),
    ("package.json", _node_test_cmd),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test"),
)

# the same markers, but the whole suite: no {test_path} placeholder. `go test`
# and `cargo test` are already whole-suite commands.
_REGRESSION_MARKERS: tuple[tuple[str, object], ...] = (
    ("pyproject.toml", DEFAULT_REGRESSION_CMD),
    ("setup.py", DEFAULT_REGRESSION_CMD),
    ("setup.cfg", DEFAULT_REGRESSION_CMD),
    ("package.json", _node_regression_cmd),
    ("go.mod", "go test ./..."),
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
