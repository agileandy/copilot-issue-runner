"""Regressions from the first live run against the real Copilot CLI.

The real binary intermittently exits 0 having written nothing to stdout. The
runner treated that empty string as a valid reply, so a planner attempt was
spent, the failure surfaced as a confusing "no parseable JSON found in reply:
''", and PlanError escaped main() as a traceback.
"""

import json
import subprocess

import pytest

from issue_runner.budget import BudgetExhausted
from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient, CopilotError


class ScriptedRunner:
    def __init__(self, stdouts):
        self.stdouts = list(stdouts)
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        stdout = self.stdouts.pop(0) if self.stdouts else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_empty_reply_is_retried_then_succeeds(tmp_path):
    runner = ScriptedRunner(["", "  \n ", "the answer"])
    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=runner)
    assert client.run("p", role="planner") == "the answer"
    assert runner.calls == 3


def test_persistent_empty_reply_raises_a_clear_error(tmp_path):
    runner = ScriptedRunner(["", "", ""])
    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=runner)
    with pytest.raises(CopilotError, match="empty reply"):
        client.run("p", role="planner")
    assert runner.calls == 3, "one initial attempt plus empty_reply_retries"


def test_empty_reply_retries_are_configurable(tmp_path):
    runner = ScriptedRunner([""] * 10)
    cfg = RunnerConfig(repo_dir=tmp_path, empty_reply_retries=0)
    with pytest.raises(CopilotError, match="empty reply"):
        CopilotClient(cfg, runner=runner).run("p", role="planner")
    assert runner.calls == 1


def test_a_good_reply_costs_exactly_one_call(tmp_path):
    runner = ScriptedRunner(["fine"])
    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=runner)
    client.run("p", role="planner")
    assert runner.calls == 1
    assert client.usage.totals()["calls"] == 1


def test_every_retry_is_accounted_in_usage(tmp_path):
    runner = ScriptedRunner(["", "ok"])
    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=runner)
    client.run("p", role="planner")
    assert client.usage.totals()["calls"] == 2, "a wasted attempt still cost time and credit"
    assert client.usage.totals()["failed"] == 1


def test_retries_respect_the_run_budget(tmp_path):
    runner = ScriptedRunner([""] * 10)
    cfg = RunnerConfig(repo_dir=tmp_path, max_run_credits=2, empty_reply_retries=5)
    with pytest.raises(BudgetExhausted):
        CopilotClient(cfg, runner=runner).run("p", role="planner")
    assert runner.calls == 2, "retries must not spend past the budget"


def test_nonzero_exit_still_raises_immediately(tmp_path):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    client = CopilotClient(RunnerConfig(repo_dir=tmp_path), runner=run)
    with pytest.raises(CopilotError, match="exited 1"):
        client.run("p", role="planner")


# --- the CLI must not show a traceback ---------------------------------------


def make_fake_copilot(tmp_path, reply):
    import stat

    fake = tmp_path / "fake-copilot"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{reply}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_planner_failure_is_reported_not_raised(tmp_path, capsys):
    from issue_runner.cli import main

    repo = tmp_path / "target"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    fake = make_fake_copilot(tmp_path, "this is not json")

    rc = main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--copilot-cmd",
            str(fake),
            "--no-github-tickets",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err


def test_valid_plan_still_runs_after_the_error_handling_change(tmp_path):
    from issue_runner.cli import main

    repo = tmp_path / "target"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    plan = json.dumps(
        {"summary": "s", "tickets": [{"title": "t", "description": "d", "test_assertion": "a"}]}
    )
    fake = make_fake_copilot(tmp_path, plan)
    rc = main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--copilot-cmd",
            str(fake),
            "--plan-only",
            "--no-github-tickets",
        ]
    )
    assert rc == 0
