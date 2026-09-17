"""The Bun viewer's model, run under this repository's own test command.

`apps/visual` is a Bun app with its own `bun:test` suite, and `uv run pytest`
cannot collect a `.ts` file. This module is the bridge: it shells out to the
viewer's real runner for a single named case, so the model's behaviour is
exercised for real and its failure output lands in the pytest report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"


def run_bun_test(name: str) -> subprocess.CompletedProcess:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's model tests — see https://bun.sh"
    return subprocess.run(
        [bun, "test", "src/model.test.ts", "-t", name],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_a_finished_run_folds_its_artefacts_into_the_summary():
    name = "a finished run folds its artefacts into the summary"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_a_finished_run_folds_the_worktree_state_into_the_summary():
    name = "a finished run folds the worktree state into the summary"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_the_metrics_section_reports_the_run_time_totals_of_a_finished_run():
    name = "the metrics section reports the run-time totals of a finished run"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_the_artefacts_section_lists_branch_pull_request_commits_and_changed_files():
    name = "the artefacts section lists the branch, pull request, commits and changed files"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"
