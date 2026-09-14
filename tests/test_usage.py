import json
import subprocess

import pytest

from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient
from issue_runner.usage import UsageLedger, format_duration


def ledger_with(*calls):
    ledger = UsageLedger()
    for kwargs in calls:
        ledger.record(**kwargs)
    return ledger


def a_call(role="planner", session=None, seconds=1.0, ok=True, usage=None, model=None):
    return {
        "role": role,
        "model": model,
        "effort": None,
        "session": session,
        "seconds": seconds,
        "ok": ok,
        "usage": usage,
    }


def test_empty_ledger_has_no_calls():
    assert UsageLedger().totals()["calls"] == 0


def test_totals_aggregate_calls_and_seconds():
    ledger = ledger_with(a_call(seconds=1.5), a_call(seconds=2.5))
    totals = ledger.totals()
    assert totals["calls"] == 2
    assert totals["seconds"] == 4.0


def test_totals_count_failures_separately():
    ledger = ledger_with(a_call(ok=True), a_call(ok=False))
    assert ledger.totals()["calls"] == 2
    assert ledger.totals()["failed"] == 1


def test_tokens_sum_when_reported():
    ledger = ledger_with(
        a_call(usage={"input_tokens": 100, "output_tokens": 10}),
        a_call(usage={"input_tokens": 50, "output_tokens": 5}),
    )
    totals = ledger.totals()
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 15


def test_tokens_are_none_when_never_reported():
    """Plain (non-streaming) mode gets no usage from copilot — don't invent zeros."""
    ledger = ledger_with(a_call(), a_call())
    assert ledger.totals()["input_tokens"] is None


def test_partial_token_reporting_sums_what_exists():
    ledger = ledger_with(a_call(usage={"input_tokens": 100}), a_call())
    assert ledger.totals()["input_tokens"] == 100
    assert ledger.totals()["output_tokens"] is None


def test_by_role_rollup():
    ledger = ledger_with(
        a_call(role="planner", seconds=2),
        a_call(role="builder.coder", seconds=3),
        a_call(role="builder.coder", seconds=4),
    )
    by_role = ledger.by_role()
    assert by_role["planner"]["calls"] == 1
    assert by_role["builder.coder"]["calls"] == 2
    assert by_role["builder.coder"]["seconds"] == 7


def test_ticket_id_is_derived_from_the_session_name():
    ledger = ledger_with(
        a_call(role="builder.tester", session="tester-t7"),
        a_call(role="verifier", session="verifier-t7"),
        a_call(role="planner", session="planner"),
    )
    assert ledger.by_ticket()[7]["calls"] == 2
    assert None not in ledger.by_ticket() or ledger.by_ticket()[None]["calls"] == 1


def test_summary_line_reports_calls_duration_and_roles():
    ledger = ledger_with(
        a_call(role="planner", seconds=5),
        a_call(role="verifier", seconds=7),
    )
    line = ledger.summary_line()
    assert "calls: 2" in line
    assert "12s" in line
    assert "planner=1" in line and "verifier=1" in line


def test_summary_line_includes_tokens_only_when_reported():
    assert "tokens" not in ledger_with(a_call()).summary_line()
    with_tokens = ledger_with(a_call(usage={"input_tokens": 1200, "output_tokens": 300}))
    assert "tokens" in with_tokens.summary_line()


def test_summary_line_flags_failures():
    assert "failed: 1" in ledger_with(a_call(ok=False)).summary_line()


def test_format_duration():
    assert format_duration(9) == "9s"
    assert format_duration(75) == "1m15s"
    assert format_duration(3725) == "1h02m05s"


# --- persistence -------------------------------------------------------------


def test_save_creates_the_usage_file(tmp_path):
    ledger = ledger_with(a_call(role="planner", seconds=3))
    path = ledger.save(tmp_path, issue_ref="17")
    assert path == tmp_path / "usage-issue-17.json"
    data = json.loads(path.read_text())
    assert data["issue_ref"] == "17"
    assert len(data["runs"]) == 1
    assert data["totals"]["calls"] == 1


def test_save_accumulates_across_resumed_runs(tmp_path):
    ledger_with(a_call(seconds=2)).save(tmp_path, issue_ref="17")
    ledger_with(a_call(seconds=3), a_call(seconds=4)).save(tmp_path, issue_ref="17")
    data = json.loads((tmp_path / "usage-issue-17.json").read_text())
    assert len(data["runs"]) == 2, "a resumed run must append, not overwrite"
    assert data["totals"]["calls"] == 3
    assert data["totals"]["seconds"] == 9


def test_save_preserves_and_reports_a_corrupt_existing_file(tmp_path):
    path = tmp_path / "usage-issue-17.json"
    path.write_text("{not json")
    with pytest.raises(OSError, match="corrupt"):
        ledger_with(a_call()).save(tmp_path, issue_ref="17")
    assert path.read_text() == "{not json"


def test_save_appends_one_jsonl_line_per_run(tmp_path):
    ledger_with(a_call()).save(tmp_path, issue_ref="17")
    ledger_with(a_call()).save(tmp_path, issue_ref="18")
    lines = (tmp_path / "usage.log").read_text().strip().splitlines()
    assert len(lines) == 2
    assert {json.loads(ln)["issue_ref"] for ln in lines} == {"17", "18"}


def test_saved_run_records_per_role_and_per_ticket_rollups(tmp_path):
    ledger = ledger_with(
        a_call(role="builder.tester", session="tester-t1", seconds=2),
        a_call(role="builder.coder", session="coder-t1", seconds=3),
    )
    ledger.save(tmp_path, issue_ref="17")
    run = json.loads((tmp_path / "usage-issue-17.json").read_text())["runs"][0]
    assert run["by_role"]["builder.tester"]["calls"] == 1
    assert run["by_ticket"]["1"]["calls"] == 2
    assert run["started_at"]


# --- client integration ------------------------------------------------------


def test_client_records_each_call(tmp_path):
    cfg = RunnerConfig(repo_dir=tmp_path)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    client = CopilotClient(cfg, runner=run)
    client.run("p", role="planner", session_name="planner")
    client.run("p", role="verifier", session_name="verifier-t1")
    assert client.usage.totals()["calls"] == 2
    assert client.usage.by_role()["verifier"]["calls"] == 1


def test_client_records_a_failed_call(tmp_path):
    import pytest

    from issue_runner.copilot import CopilotError

    cfg = RunnerConfig(repo_dir=tmp_path)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    client = CopilotClient(cfg, runner=run)
    with pytest.raises(CopilotError):
        client.run("p", role="planner")
    assert client.usage.totals()["calls"] == 1
    assert client.usage.totals()["failed"] == 1


def test_client_records_a_timeout(tmp_path):
    import pytest

    from issue_runner.copilot import CopilotError

    cfg = RunnerConfig(repo_dir=tmp_path)

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    client = CopilotClient(cfg, runner=run)
    with pytest.raises(CopilotError):
        client.run("p", role="planner")
    assert client.usage.totals()["failed"] == 1


def test_budget_refusal_is_not_recorded_as_a_call(tmp_path):
    import pytest

    from issue_runner.budget import BudgetExhausted

    cfg = RunnerConfig(repo_dir=tmp_path, max_run_credits=1)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    client = CopilotClient(cfg, runner=run)
    client.run("p", role="planner")
    with pytest.raises(BudgetExhausted):
        client.run("p", role="planner")
    assert client.usage.totals()["calls"] == 1


# --- orchestrator + CLI integration ------------------------------------------


def test_run_writes_a_usage_file_and_summary(git_repo, cfg):
    from issue_runner.orchestrator import run_issue

    plan = json.dumps(
        {"summary": "s", "tickets": [{"title": "t", "description": "d", "test_assertion": "a"}]}
    )

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=plan, stderr="")

    client = CopilotClient(cfg, runner=run)
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state", plan_only=True)
    # plan-only still spends a planner call, so it must still be accounted
    assert "calls: 1" in report.usage_summary
    data = json.loads((git_repo / ".state" / "usage-issue-17.json").read_text())
    assert data["totals"]["calls"] == 1
    assert data["runs"][0]["by_role"]["planner"]["calls"] == 1


def test_usage_file_written_even_when_the_run_blocks(git_repo, cfg):
    from issue_runner.orchestrator import run_issue
    from tests.conftest import FakeClient

    class Accounted(FakeClient):
        def __init__(self, script):
            super().__init__(script)
            self.usage = UsageLedger()

        def run(self, prompt, role, read_only=False, session_name=None):
            self.usage.record(
                role=role,
                model=None,
                effort=None,
                session=session_name,
                seconds=1,
                ok=True,
                usage=None,
            )
            return super().run(prompt, role, read_only, session_name)

    plan = json.dumps(
        {"summary": "s", "tickets": [{"title": "t", "description": "d", "test_assertion": "a"}]}
    )
    client = Accounted([(plan, None)])  # tester call exhausts the script -> ticket blocks
    report = run_issue(cfg, client, ISSUE, state_dir=git_repo / ".state", plan_only=True)
    assert (git_repo / ".state" / "usage-issue-17.json").exists()
    assert "calls: 1" in report.usage_summary


def test_a_client_without_a_ledger_does_not_break_the_run(git_repo, cfg):
    """FakeClients in the existing suite have no .usage — accounting is optional."""
    from issue_runner.orchestrator import run_issue
    from tests.conftest import FakeClient

    plan = json.dumps(
        {"summary": "s", "tickets": [{"title": "t", "description": "d", "test_assertion": "a"}]}
    )
    report = run_issue(
        cfg, FakeClient([(plan, None)]), ISSUE, state_dir=git_repo / ".state", plan_only=True
    )
    assert report.usage_summary == ""
    assert not (git_repo / ".state" / "usage-issue-17.json").exists()


ISSUE = {"number": 17, "title": "Add subtract", "body": "need it", "url": ""}


@pytest.fixture
def git_repo(repo):
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "runner@test.local")
    git("config", "user.name", "Runner Test")
    git("add", "-A")
    git("commit", "-m", "seed")
    return repo
