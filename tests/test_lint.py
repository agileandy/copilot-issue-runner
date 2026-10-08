import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_ruff_dev_pin_matches_lock():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())

    ruff_spec = next(
        dep for dep in pyproject["dependency-groups"]["dev"] if dep.startswith("ruff")
    )
    locked_version = next(
        pkg["version"] for pkg in lock["package"] if pkg["name"] == "ruff"
    )

    assert ruff_spec == f"ruff=={locked_version}"


def test_ruff_check_is_clean():
    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_ruff_lints_nested_factory_dir():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--force-exclude",
            "--stdin-filename",
            "src/issue_runner/factory/x.py",
            "-",
        ],
        cwd=ROOT,
        input="import os\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert "F401" in result.stdout, result.stdout + result.stderr
