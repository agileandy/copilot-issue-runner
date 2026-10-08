"""The Bun viewer's key mapping, run under this repository's own test command.

`apps/visual/src/keys.test.ts` exercises `keyAction` for real, and
`uv run pytest` cannot collect a `.ts` file. This module is the bridge: it
shells out to the viewer's real runner for a single named case, so the mapping
is exercised for real and its failure output lands in the pytest report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"


def run_bun_test(name: str) -> subprocess.CompletedProcess:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's key tests — see https://bun.sh"
    return subprocess.run(
        [bun, "test", "src/keys.test.ts", "-t", name],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_s_saves_the_summary_while_the_summary_overlay_is_open():
    name = "s saves the summary while the summary overlay is open"
    result = run_bun_test(name)
    assert result.returncode == 0, f"bun test {name!r} failed:\n{result.stdout}{result.stderr}"
