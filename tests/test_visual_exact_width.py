"""Lines of exactly the agent text-area width keep the newest text in view.

The frame behaviour lives in `apps/visual/src/app.test.ts`, which drives the real
app against a test renderer; `uv run pytest` cannot collect a `.ts` file. This
module shells out to the viewer's real runner for one named case, so the
behaviour is exercised for real and its failure output lands in the pytest report.
"""

import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "visual"
CASE = "lines of exactly the text-area width keep the newest text in view"


def test_lines_of_exactly_the_text_area_width_keep_the_newest_text_in_view():
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to run the viewer's frame tests — see https://bun.sh"

    result = subprocess.run(
        [bun, "test", "src/app.test.ts", "-t", CASE],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    output = result.stdout + result.stderr
    assert "matched 0 tests" not in output, f"the named case never ran:\n{output}"

    assert result.returncode == 0, f"bun test {CASE!r} failed:\n{output}"
