"""The --demo sandbox must run the whole pipeline offline, with no model call."""

import json
import subprocess
import sys

import pytest

from issue_runner import demo as demo_module
from issue_runner.cli import main
from issue_runner.demo import DemoError, demo_test_cmd, minitest, setup_demo
from issue_runner.demo.responder import respond


def test_setup_demo_creates_a_committed_git_repo(tmp_path):
    env = setup_demo(tmp_path / "sandbox")

    assert env.issue_file.is_file()
    assert env.copilot_cmd.is_file()
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=env.repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "demo sandbox skeleton" in log.stdout


def test_setup_demo_refuses_a_non_empty_directory_unless_reset(tmp_path):
    dest = tmp_path / "sandbox"
    setup_demo(dest)
    with pytest.raises(DemoError):
        setup_demo(dest)
    assert setup_demo(dest, force=True).repo_dir == dest


def test_demo_test_cmd_falls_back_when_pytest_is_absent(monkeypatch):
    """A `uv tool install` venv has no pytest; the demo must still run green."""
    assert "pytest" in demo_test_cmd()
    monkeypatch.setattr(demo_module.importlib.util, "find_spec", lambda name: None)
    fallback = demo_test_cmd()
    assert "minitest.py" in fallback and "pytest" not in fallback


def test_minitest_reports_red_and_green(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    green = tmp_path / "test_green.py"
    green.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    red = tmp_path / "test_red.py"
    red.write_text("def test_bad():\n    assert 1 + 1 == 3\n")

    assert minitest.main([str(green)]) == 0
    assert minitest.main([str(red)]) == 1
    assert minitest.main([str(tmp_path / "missing.py")]) == 2


def test_responder_drives_the_ticket_to_green_only_after_a_retry(tmp_path):
    """The scripted coder is wrong first: that is what shows the harness working."""
    prompt = "You are builder.coder\nSub-task: add median to demo_pkg.stats\n"

    first, _, _ = respond(prompt, tmp_path)
    stats = (tmp_path / "demo_pkg" / "stats.py").read_text()
    assert "sorted" not in stats  # the deliberate first-attempt bug

    respond(prompt, tmp_path)
    respond(prompt, tmp_path)
    assert "sorted" in (tmp_path / "demo_pkg" / "stats.py").read_text()
    assert json.loads(first)["changed_files"] == ["demo_pkg/stats.py"]


def test_responder_streams_copilot_json_events(tmp_path):
    env = setup_demo(tmp_path / "sandbox")
    result = subprocess.run(
        [
            str(env.copilot_cmd),
            "-p",
            "You are the planner in an automated TDD pipeline",
            "--output-format",
            "json",
            "-C",
            str(env.repo_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "ISSUE_RUNNER_DEMO_DELAY": "0"},
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert "assistant.message" in kinds
    assert kinds[-1] == "model.model_call_success"
    message = next(e for e in events if e["type"] == "assistant.message")
    assert len(json.loads(message["data"]["content"])["tickets"]) == 2


def test_demo_runs_the_whole_pipeline_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ISSUE_RUNNER_DEMO_DELAY", "0")
    rc = main(["--demo", "--demo-dir", str(tmp_path / "sandbox")])
    out = capsys.readouterr().out

    assert rc == 0
    assert "tickets done: 2, blocked: 0" in out
    repo = tmp_path / "sandbox"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "ticket-1" in log and "ticket-2" in log
    # the median implementation the scripted loop converged on must be correct
    proof = subprocess.run(
        [sys.executable, "-c", "from demo_pkg.stats import median; print(median([1, 2, 3, 4]))"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proof.stdout.strip() == "2.5"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert dirty.strip() == ""
