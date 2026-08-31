import subprocess

import pytest

from issue_runner.budget import BudgetExhausted, RunBudget
from issue_runner.config import RunnerConfig, load_config
from issue_runner.copilot import CopilotClient


def test_unlimited_budget_never_raises():
    budget = RunBudget(limit=None, per_call=99)
    for _ in range(50):
        budget.check()
        budget.charge()
    assert budget.remaining is None


def test_budget_allows_exactly_the_affordable_calls():
    budget = RunBudget(limit=3, per_call=1)
    for _ in range(3):
        budget.check()
        budget.charge()
    assert budget.remaining == 0
    with pytest.raises(BudgetExhausted):
        budget.check()


def test_budget_uses_per_call_estimate():
    budget = RunBudget(limit=60, per_call=30)
    budget.check()
    budget.charge()
    budget.check()
    budget.charge()
    with pytest.raises(BudgetExhausted):
        budget.check()
    assert budget.calls == 2


def test_budget_never_overspends_when_limit_is_not_a_multiple():
    budget = RunBudget(limit=50, per_call=30)
    budget.check()
    budget.charge()
    with pytest.raises(BudgetExhausted):
        budget.check()
    assert budget.spent == 30


def test_per_call_is_at_least_one():
    assert RunBudget(limit=2, per_call=0).per_call == 1


def _fake_runner(replies):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=replies.pop(0), stderr="")

    return run


def test_client_stops_before_exceeding_run_budget(tmp_path):
    cfg = RunnerConfig(repo_dir=tmp_path, max_run_credits=2)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    client = CopilotClient(cfg, runner=run)
    assert client.run("p", role="planner") == "ok"
    assert client.run("p", role="planner") == "ok"
    with pytest.raises(BudgetExhausted):
        client.run("p", role="planner")
    assert len(calls) == 2, "the third call must never reach the copilot binary"


def test_client_without_budget_is_unchanged(tmp_path):
    cfg = RunnerConfig(repo_dir=tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    client = CopilotClient(cfg, runner=run)
    for _ in range(5):
        client.run("p", role="planner")
    assert len(calls) == 5
    assert client.budget.limit is None


def test_client_budget_uses_max_ai_credits_as_the_estimate(tmp_path):
    cfg = RunnerConfig(repo_dir=tmp_path, max_ai_credits=30, max_run_credits=60)
    client = CopilotClient(cfg)
    assert client.budget.per_call == 30
    assert client.budget.limit == 60


def test_max_run_credits_defaults_to_unset_and_loads_from_toml(tmp_path):
    assert RunnerConfig(repo_dir=tmp_path).max_run_credits is None
    (tmp_path / "runner.toml").write_text("max_run_credits = 120\n")
    assert load_config(tmp_path).max_run_credits == 120
