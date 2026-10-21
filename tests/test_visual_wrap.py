"""The Bun viewer's text wrapping, run under this repository's own test command.

`apps/visual/src/wrap.test.ts` exercises `wrapText` for real, and
`uv run pytest` cannot collect a `.ts` file. This module is the bridge: it
shells out to the viewer's real runner for a single named case, so the wrapping
is exercised for real and its failure output lands in the pytest report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"


def run_bun_test(name: str) -> subprocess.CompletedProcess:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's wrap tests — see https://bun.sh"
    return subprocess.run(
        [bun, "test", "src/wrap.test.ts", "-t", name],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def test_wrap_text_breaks_at_spaces_and_keeps_existing_newlines_and_blank_lines():
    name = "wrapText breaks at spaces and keeps existing newlines and blank lines"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"


def test_wrap_text_hard_breaks_a_word_longer_than_the_width():
    name = "wrapText hard-breaks a word longer than the width into width-sized chunks"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"
