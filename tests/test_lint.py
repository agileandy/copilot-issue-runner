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
