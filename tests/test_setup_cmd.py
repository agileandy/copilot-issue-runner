"""setup_cmd: the repository's own worktree setup, in place of discovered steps."""

import sys
from pathlib import Path

import pytest

from issue_runner import toolchain
from issue_runner.config import ConfigError, load_config, validate_config
from issue_runner.orchestrator import run_issue
from issue_runner.toolchain import ProvisionError
from tests.hardening_support import ISSUE, DemoClient, git, sandbox

PY = sys.executable


def _write_cmd(path: str) -> str:
    return (
        f"{PY} -c \"import os; os.makedirs(os.path.dirname('{path}') or '.', exist_ok=True); "
        f"open('{path}', 'w').write('ok')\""
    )


def test_setup_cmd_loads_from_toml_as_a_string_or_a_list(tmp_path):
    (tmp_path / "runner.toml").write_text('setup_cmd = "make deps"\n')
    assert load_config(tmp_path).setup_cmd == ["make deps"]
    (tmp_path / "runner.toml").write_text('setup_cmd = ["uv venv", "uv pip install x"]\n')
    assert load_config(tmp_path).setup_cmd == ["uv venv", "uv pip install x"]


def test_setup_cmd_must_hold_commands(tmp_path):
    (tmp_path / "runner.toml").write_text("setup_cmd = [1]\n")
    with pytest.raises(ConfigError, match="setup_cmd"):
        validate_config(load_config(tmp_path))


def test_setup_cmd_replaces_discovery_and_prepares_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain, "provision", lambda *a: pytest.fail("discovery must not run"))
    env, cfg = sandbox(tmp_path, isolated=True, setup_cmd=[_write_cmd(".venv/ran")])
    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    workspace = Path(report.worktree)
    assert (report.done, report.blocked) == (1, 0)
    assert (workspace / ".venv" / "ran").read_text() == "ok"
    assert not (env.repo_dir / ".venv").exists()
    assert git(workspace, "status", "--porcelain") == ""


def test_setup_cmd_runs_even_when_provisioning_is_off(tmp_path):
    _env, cfg = sandbox(
        tmp_path, isolated=True, provision=False, setup_cmd=[_write_cmd(".venv/ran")]
    )
    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    assert (Path(report.worktree) / ".venv" / "ran").exists()


def test_a_failed_setup_cmd_stops_the_run_before_any_model_call(tmp_path):
    _env, cfg = sandbox(
        tmp_path, isolated=True, setup_cmd=[f"{PY} -c \"import sys; sys.exit('no deps here')\""]
    )
    client = DemoClient(cfg)
    with pytest.raises(ProvisionError, match="no deps here"):
        run_issue(cfg, client, ISSUE)
    assert client.roles == []


def test_a_setup_cmd_that_writes_a_tracked_path_is_refused(tmp_path):
    _env, cfg = sandbox(tmp_path, isolated=True, setup_cmd=[_write_cmd("stray.txt")])
    with pytest.raises(ProvisionError, match="stray.txt"):
        run_issue(cfg, DemoClient(cfg), ISSUE)


def test_discovered_provisioning_that_writes_a_tracked_path_is_refused(tmp_path, monkeypatch):
    def careless(root, commands, timeout):
        (Path(root) / "stray.txt").write_text("oops")

    monkeypatch.setattr(toolchain, "provision", careless)
    _env, cfg = sandbox(tmp_path, isolated=True)
    with pytest.raises(ProvisionError, match="changed files in the run worktree: stray.txt"):
        run_issue(cfg, DemoClient(cfg), ISSUE)


def test_a_setup_cmd_that_cannot_be_parsed_or_started_is_named(tmp_path):
    with pytest.raises(ProvisionError, match="could not be parsed"):
        toolchain.run_setup(tmp_path, ['echo "unterminated'], 5)
    with pytest.raises(ProvisionError, match="could not start"):
        toolchain.run_setup(tmp_path, ["no-such-binary-for-issue-runner"], 5)
