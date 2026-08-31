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


# ordered: the first marker that matches wins in a polyglot repo
_MARKERS: tuple[tuple[str, object], ...] = (
    ("pyproject.toml", DEFAULT_TEST_CMD),
    ("setup.py", DEFAULT_TEST_CMD),
    ("setup.cfg", DEFAULT_TEST_CMD),
    ("package.json", _node_test_cmd),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test"),
)


def detect_test_cmd(repo_dir: Path) -> Detection:
    repo_dir = Path(repo_dir)
    for name, recipe in _MARKERS:
        path = repo_dir / name
        if not path.is_file():
            continue
        test_cmd = recipe(path) if callable(recipe) else recipe
        if test_cmd:
            return Detection(test_cmd, name)
    return Detection(DEFAULT_TEST_CMD, None)
