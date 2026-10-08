"""A stopped run is held on the summary overlay, not closed out from under the user.

The frame behaviour lives in `apps/visual/src/app.test.ts`, which drives the real
app against a test renderer; `uv run pytest` cannot collect a `.ts` file. This
module is the bridge used everywhere else in this suite: it shells out to the
viewer's real runner for one named case, so the behaviour is exercised for real
and its failure output lands in the pytest report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"
CASE = "a stopped run is held on the summary overlay with its dismiss keys"


def test_a_stopped_run_is_held_on_the_summary_overlay_with_its_dismiss_keys():
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's frame tests — see https://bun.sh"

    result = subprocess.run(
        [bun, "test", "src/app.test.ts", "-t", CASE],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    output = result.stdout + result.stderr
    assert "matched 0 tests" not in output, f"the named case never ran:\n{output}"

    assert result.returncode == 0, f"bun test {CASE!r} failed:\n{output}"
