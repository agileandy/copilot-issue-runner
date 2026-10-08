import json

import pytest

from issue_runner import orchestrator
from issue_runner.orchestrator import run_issue
from tests.conftest import FakeClient
from tests.hardening_support import ISSUE, git, load_store, sandbox


def regression_scenario(tmp_path, *, break_existing=True, verifier_mutation=False):
    env, cfg = sandbox(tmp_path, max_rounds=0)
    repo = env.repo_dir
    (repo / "feature.py").write_text(
        "def existing():\n    return 1\n\ndef added():\n    return 0\n"
    )
    (repo / "tests" / "test_existing.py").write_text(
        "from feature import existing\n\ndef test_existing():\n    assert existing() == 1\n"
    )
    git(repo, "add", "feature.py", "tests/test_existing.py")
    git(repo, "commit", "-m", "test: seed existing behavior")

    def test():
        (repo / "tests" / "test_added.py").write_text(
            "from feature import added\n\ndef test_added():\n    assert added() == 42\n"
        )

    def code():
        existing = 0 if break_existing else 1
        (repo / "feature.py").write_text(
            f"def existing():\n    return {existing}\n\ndef added():\n    return 42\n"
        )

    def mutate():
        (repo / "unreviewed.py").write_text("VALUE = 99\n")

    plan = {
        "summary": "Add behavior",
        "tickets": [{"title": "add", "description": "d", "test_assertion": "added() == 42"}],
    }
    script = [
        (json.dumps(plan), None),
        (json.dumps({"test_path": "tests/test_added.py"}), test),
        ('{"changed_files":["feature.py"]}', code),
        (
            '{"verdict":"pass","reasons":["target passes"]}',
            mutate if verifier_mutation else None,
        ),
    ]
    return env, cfg, FakeClient(script)


def test_existing_test_failure_prevents_commit_and_publication(tmp_path, monkeypatch):
    env, cfg, client = regression_scenario(tmp_path)
    before = git(env.repo_dir, "rev-parse", "HEAD")
    cfg.repo = "owner/repo"
    cfg.open_pr = True
    published = []
    monkeypatch.setattr(
        "issue_runner.orchestrator._open_pull_request", lambda *args: published.append(1)
    )

    report = run_issue(cfg, client, ISSUE)
    assert (report.done, report.blocked) == (0, 1)
    blocked = load_store(env).tickets[0]
    assert "regression gate failed" in blocked.blocked_reason
    assert blocked.blocked_stage == "regression"  # where it failed, not where it was sent back
    assert git(env.repo_dir, "rev-parse", "HEAD") == before
    assert published == []


def test_a_ticket_is_accepted_when_target_and_existing_tests_pass(tmp_path):
    env, cfg, client = regression_scenario(tmp_path, break_existing=False)
    report = run_issue(cfg, client, ISSUE)
    assert (report.done, report.blocked) == (1, 0)
    assert load_store(env).tickets[0].commit_sha == git(env.repo_dir, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "command",
    ["python -c 'print(\"no test evidence\")'", "python -m pytest missing-test.py -q"],
)
def test_invalid_regression_execution_cannot_accept_work(tmp_path, command):
    env, cfg, client = regression_scenario(tmp_path, break_existing=False)
    cfg.regression_cmd = command
    before = git(env.repo_dir, "rev-parse", "HEAD")
    report = run_issue(cfg, client, ISSUE)
    assert (report.done, report.blocked) == (0, 1)
    assert git(env.repo_dir, "rev-parse", "HEAD") == before


def test_verifier_cannot_change_the_files_it_approved(tmp_path):
    env, cfg, client = regression_scenario(tmp_path, break_existing=False, verifier_mutation=True)
    report = run_issue(cfg, client, ISSUE)
    assert (report.done, report.blocked) == (0, 1)
    assert "read-only verifier modified" in load_store(env).tickets[0].blocked_reason
    assert (env.repo_dir / "unreviewed.py").read_text() == "VALUE = 99\n"
    assert "unreviewed.py" not in git(env.repo_dir, "ls-files")


def test_new_files_only_run_focused_tests_per_ticket_then_full_suite_once(tmp_path, monkeypatch):
    env, cfg = sandbox(tmp_path, max_rounds=0)
    repo = env.repo_dir

    def test():
        (repo / "tests" / "test_added.py").write_text(
            "def test_added():\n    from added import added\n\n    assert added() == 42\n"
        )

    def code():
        (repo / "added.py").write_text("def added():\n    return 42\n")

    plan = {
        "summary": "Add behavior",
        "tickets": [{"title": "add", "description": "d", "test_assertion": "added() == 42"}],
    }
    client = FakeClient(
        [
            (json.dumps(plan), None),
            (json.dumps({"test_path": "tests/test_added.py"}), test),
            ('{"changed_files":["added.py"]}', code),
            ('{"verdict":"pass","reasons":["target passes"]}', None),
        ]
    )
    real = orchestrator.run_test_command
    commands = []

    def recording(cfg_arg, command):
        commands.append(command)
        return real(cfg_arg, command)

    monkeypatch.setattr("issue_runner.orchestrator.run_test_command", recording)

    run_issue(cfg, client, ISSUE)

    assert commands == [
        cfg.test_cmd.format(test_path="tests/test_added.py"),
        cfg.regression_cmd,
    ]


def test_focused_ticket_regression_runs_focused_tests_even_for_shared_changes(
    tmp_path, monkeypatch
):
    _env, cfg, client = regression_scenario(tmp_path, break_existing=False)
    cfg.ticket_regression = "focused"
    real = orchestrator.run_test_command
    commands = []

    def recording(cfg_arg, command):
        commands.append(command)
        return real(cfg_arg, command)

    monkeypatch.setattr("issue_runner.orchestrator.run_test_command", recording)

    run_issue(cfg, client, ISSUE)

    assert commands == [
        cfg.test_cmd.format(test_path="tests/test_added.py tests/test_existing.py"),
        cfg.regression_cmd,
    ]


def test_full_ticket_regression_runs_full_suite_per_ticket_for_new_files_only(
    tmp_path, monkeypatch
):
    env, cfg = sandbox(tmp_path, max_rounds=0)
    cfg.ticket_regression = "full"
    repo = env.repo_dir

    def test():
        (repo / "tests" / "test_added.py").write_text(
            "def test_added():\n    from added import added\n\n    assert added() == 42\n"
        )

    def code():
        (repo / "added.py").write_text("def added():\n    return 42\n")

    plan = {
        "summary": "Add behavior",
        "tickets": [{"title": "add", "description": "d", "test_assertion": "added() == 42"}],
    }
    client = FakeClient(
        [
            (json.dumps(plan), None),
            (json.dumps({"test_path": "tests/test_added.py"}), test),
            ('{"changed_files":["added.py"]}', code),
            ('{"verdict":"pass","reasons":["target passes"]}', None),
        ]
    )
    real = orchestrator.run_test_command
    commands = []

    def recording(cfg_arg, command):
        commands.append(command)
        return real(cfg_arg, command)

    monkeypatch.setattr("issue_runner.orchestrator.run_test_command", recording)

    run_issue(cfg, client, ISSUE)

    assert commands == [cfg.regression_cmd, cfg.regression_cmd]


def test_shared_change_full_ticket_gate_skips_duplicate_final_gate(tmp_path, monkeypatch):
    _env, cfg, client = regression_scenario(tmp_path, break_existing=False)
    real = orchestrator.run_test_command
    commands = []

    def recording(cfg_arg, command):
        commands.append(command)
        return real(cfg_arg, command)

    monkeypatch.setattr("issue_runner.orchestrator.run_test_command", recording)

    run_issue(cfg, client, ISSUE)

    assert commands == [cfg.regression_cmd]


def test_failing_focused_gate_hands_the_ticket_back(tmp_path):
    env, cfg, client = regression_scenario(tmp_path)
    cfg.ticket_regression = "focused"

    report = run_issue(cfg, client, ISSUE)

    blocked_reason = load_store(env).tickets[0].blocked_reason
    assert (
        "regression gate failed; repair existing behaviour" in blocked_reason
        and (report.done, report.blocked) == (0, 1)
    )
