import json
import sys
from pathlib import Path

import pytest

from issue_runner.config import ConfigError, load_config, validate_config
from issue_runner.envsetup import SetupError, detect_setup
from issue_runner.orchestrator import run_issue
from tests.hardening_support import ISSUE, DemoClient, git, sandbox

PY = sys.executable


def write(repo: Path, name: str, text: str = "") -> None:
    (repo / name).write_text(text)


def test_uv_lock_syncs_frozen(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "uv.lock")
    assert detect_setup(tmp_path) == (["uv sync --frozen"], "uv.lock")


def test_python_without_a_lock_installs_project_requirements_and_pytest(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "requirements.txt")
    write(tmp_path, "requirements-dev.txt")
    commands, marker = detect_setup(tmp_path)
    assert marker == "pyproject.toml without a lock file"
    assert commands == [
        "uv venv --allow-existing .venv",
        (
            "uv pip install --python .venv/bin/python -e . "
            "-r requirements-dev.txt -r requirements.txt pytest"
        ),
    ]


def test_a_tool_only_pyproject_is_not_installed_as_a_package(tmp_path):
    write(tmp_path, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    commands, _ = detect_setup(tmp_path)
    assert commands[-1] == "uv pip install --python .venv/bin/python pytest"


@pytest.mark.parametrize(
    "lock, command",
    [
        ("package-lock.json", "npm ci"),
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
        ("bun.lock", "bun install --frozen-lockfile"),
    ],
)
def test_node_lock_files_install_frozen(tmp_path, lock, command):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "vitest"}}))
    write(tmp_path, lock)
    assert detect_setup(tmp_path) == ([command], lock)


@pytest.mark.parametrize("marker", ["go.mod", "Cargo.toml"])
def test_toolchains_that_fetch_their_own_dependencies_need_no_setup(tmp_path, marker):
    write(tmp_path, marker)
    assert detect_setup(tmp_path) == ([], None)


def test_node_without_a_lock_file_is_not_installed(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "vitest"}}))
    assert detect_setup(tmp_path) == ([], None)


def test_setup_cmd_loads_from_toml_as_a_string_or_a_list(tmp_path):
    write(tmp_path, "runner.toml", 'setup_cmd = "make deps"\n')
    assert load_config(tmp_path).setup_cmd == ["make deps"]
    write(tmp_path, "runner.toml", 'setup_cmd = ["uv venv", "uv pip install x"]\n')
    assert load_config(tmp_path).setup_cmd == ["uv venv", "uv pip install x"]


def test_setup_cmd_must_hold_commands(tmp_path):
    write(tmp_path, "runner.toml", "setup_cmd = [1]\n")
    with pytest.raises(ConfigError, match="setup_cmd"):
        validate_config(load_config(tmp_path))


def _write_cmd(path: str) -> str:
    return (
        f"{PY} -c \"import os; os.makedirs(os.path.dirname('{path}') or '.', exist_ok=True); "
        f"open('{path}', 'w').write('ok')\""
    )


def test_setup_cmd_prepares_the_run_worktree_before_the_run(tmp_path):
    env, cfg = sandbox(tmp_path, isolated=True, setup_cmd=[_write_cmd(".venv/ran")])
    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    workspace = Path(report.worktree)
    assert (report.done, report.blocked) == (1, 0)
    assert (workspace / ".venv" / "ran").read_text() == "ok"
    assert not (env.repo_dir / ".venv").exists()
    assert git(workspace, "status", "--porcelain") == ""


def test_failed_setup_stops_the_run_before_any_model_call(tmp_path):
    _env, cfg = sandbox(
        tmp_path, isolated=True, setup_cmd=[f"{PY} -c \"import sys; sys.exit('no deps here')\""]
    )
    client = DemoClient(cfg)
    with pytest.raises(SetupError, match="no deps here"):
        run_issue(cfg, client, ISSUE)
    assert client.roles == []


def test_setup_that_writes_tracked_paths_is_refused(tmp_path):
    _env, cfg = sandbox(tmp_path, isolated=True, setup_cmd=[_write_cmd("stray.txt")])
    with pytest.raises(SetupError, match="stray.txt"):
        run_issue(cfg, DemoClient(cfg), ISSUE)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_a_detected_test_cmd_is_redetected_in_the_run_worktree(tmp_path):
    # a stand-in worktree venv whose python is the interpreter running these tests
    wrapper = tmp_path / "make_venv.py"
    wrapper.write_text(
        "import os, sys\n"
        "os.makedirs('.venv/bin')\n"
        f"open('.venv/bin/python', 'w').write('#!/bin/sh\\nexec {PY} \\\"$@\\\"\\n')\n"
        "os.chmod('.venv/bin/python', 0o755)\n"
    )
    _env, cfg = sandbox(tmp_path, isolated=True, setup_cmd=[f"{PY} {wrapper}"])
    cfg.test_cmd_detected = True
    original = cfg.test_cmd
    seen = []

    class Recording(DemoClient):
        def run(self, prompt, role, **kwargs):
            seen.append(cfg.test_cmd)
            return super().run(prompt, role=role, **kwargs)

    report = run_issue(cfg, Recording(cfg), ISSUE)
    workspace_python = str(Path(report.worktree) / ".venv" / "bin" / "python")
    assert (report.done, report.blocked) == (1, 0)
    assert seen and all(cmd.startswith(workspace_python) for cmd in seen)
    assert cfg.test_cmd == original


def test_an_explicit_test_cmd_skips_automatic_setup(tmp_path):
    _env, cfg = sandbox(tmp_path, isolated=True)
    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    assert not (Path(report.worktree) / ".venv").exists()
