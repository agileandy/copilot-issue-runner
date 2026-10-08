import subprocess
from pathlib import Path

import pytest

import issue_runner.phases.build as build

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = "src/issue_runner/phases/build.py"


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_pr_leaves_build_py_as_on_main_with_no_unused_no_tests_error():
    # origin/main can move ahead of the branch, so compare against the point the
    # branch forked from: that diff is exactly what this PR changes in build.py.
    base = _git("merge-base", "HEAD", "origin/main")
    if base.returncode != 0:
        pytest.skip(f"origin/main is not available: {base.stderr.strip()}")
    diff = _git("diff", base.stdout.strip(), "--", BUILD_PY)
    assert diff.returncode == 0, diff.stderr

    assert (hasattr(build, "NoTestsError"), diff.stdout) == (False, "")
