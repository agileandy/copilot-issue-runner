import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# #151 covers only the UV_NO_SYNC change. These files must match origin/main.
OUT_OF_SCOPE = [
    "src/issue_runner/cli.py",
    "src/issue_runner/journal.py",
    "tests/test_visual_app.py",
    "tests/test_visual_keys.py",
    "tests/test_visual_model.py",
    "tests/test_visual_save.py",
    "tests/test_visual_stopped_summary.py",
]


def test_out_of_scope_ruff_edits_are_reverted():
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if base.returncode != 0:
        pytest.fail("origin/main is not available in this checkout")
    diff = subprocess.run(
        ["git", "diff", "--quiet", "origin/main", "--", *OUT_OF_SCOPE],
        cwd=REPO,
        check=False,
    )
    assert diff.returncode == 0
