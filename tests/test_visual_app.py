"""The Bun viewer's rendered frames, run under this repository's own test command.

`apps/visual/src/app.test.ts` drives the real app against a test renderer, and
`uv run pytest` cannot collect a `.ts` file. This module is the bridge: it
shells out to the viewer's real runner for a single named case, so the frame
behaviour is exercised for real and its failure output lands in the pytest
report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"


def run_bun_test(name: str) -> subprocess.CompletedProcess:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's frame tests — see https://bun.sh"
    return subprocess.run(
        [bun, "test", "src/app.test.ts", "-t", name],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_the_finished_run_shows_its_summary_as_a_modal_over_the_panes():
    name = "the finished run shows its summary as a modal over the panes"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_the_summary_scrolls_to_the_bottom_of_a_long_artefact_list():
    name = "the summary scrolls to the bottom of a long artefact list"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_the_summary_stays_open_when_later_events_arrive():
    name = "the summary stays open when later events arrive"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"
