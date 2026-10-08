import json
import logging
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from issue_runner.cli import build_parser, gh_main, main
from issue_runner.visual import _visual_snapshot_for_non_tty, render_flow


@pytest.fixture(autouse=True)
def restore_logging_after_cli():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def make_fake_copilot(tmp_path, reply):
    """A stand-in copilot binary: ignores args, prints a canned reply."""
    fake = tmp_path / "fake-copilot"
    event = json.dumps({"type": "assistant.message", "data": {"content": reply}})
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{event}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def git_init(repo):
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_plan_only_end_to_end_with_fake_binary(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
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
    out = capsys.readouterr().out
    assert "subtract ints" in out
    state_files = list((repo / ".issue-runner").glob("issue-*.json"))
    assert len(state_files) == 1
    assert "sub(5,3)==2" in state_files[0].read_text()


def test_dry_run_makes_no_calls(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nB")

    rc = main(["--issue-file", str(issue_file), "--dir", str(repo), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "copilot" in out and "--allow-all-tools" in out
    assert not (repo / ".issue-runner").exists()


def test_verbose_plan_logging_uses_debug_stream(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
    )
    make_fake_copilot(tmp_path, plan)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--plan-only",
            "--no-github-tickets",
            "-v",
        ]
    )
    assert "plan: 1 tickets" in capsys.readouterr().err


def test_verbose_ticket_verdict_logging_in_build_verify_loop(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add ints\n\nNeed a function to add integers.")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ticket_flow.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "add-example"\nversion = "0.1.0"\n')
    for args in (["add", "-A"], ["commit", "-m", "test: seed failing add behavior"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    fake = tmp_path / "fake-copilot"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
        "if 'You are the planner' in prompt:\n"
        "    reply = {'summary': 'one ticket', 'tickets': [{'title': 'add ints',\n"
        "        'description': 'implement add', 'test_assertion': 'add(2,3) == 5'}]}\n"
        "elif 'You are builder.tester' in prompt:\n"
        "    reply = {'test_path': 'tests/test_ticket_flow.py'}\n"
        "elif 'You are builder.coder' in prompt:\n"
        "    Path('app.py').write_text('def add(a,b):\\n    return a+b\\n')\n"
        "    reply = {'changed_files': ['app.py']}\n"
        "else:\n"
        "    reply = {'verdict': 'pass', 'reasons': ['works']}\n"
        "print(json.dumps({'type': 'assistant.message', 'data': {'content': json.dumps(reply)}}))\n"
    )
    fake.chmod(fake.stat().st_mode | 0o111)

    main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--copilot-cmd",
            str(fake),
            "--no-github-tickets",
            "-v",
        ]
    )
    assert "ticket 1 verdict: pass" in capsys.readouterr().err


def test_visual_flag_sets_boolean():
    args = build_parser().parse_args(["--visual"])
    assert args.visual is True


def test_visual_snapshot_falls_back_to_plain_text_for_non_tty(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert "plan" in render_flow(
        {"plan": "done", "branch": "pending"}
    ) or "plan" in _visual_snapshot_for_non_tty({"plan": "done", "branch": "pending"})


def test_requires_issue_ref_or_file(tmp_path, capsys):
    rc = main(["--dir", str(tmp_path)])
    assert rc == 2


def test_gh_main_delegates_to_main(tmp_path, capsys):
    rc = gh_main(["--dir", str(tmp_path)])
    assert rc == 2


def test_gr_runner_script_registered_in_pyproject():
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["gh-runner"] == "issue_runner.cli:gh_main"


def test_gitea_origin_routes_to_gitea_fetch(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", "http://gitea.local:3000/Org/thing.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    fetched = {}

    def fake_fetch(api_base, owner_repo, number, token=None, getter=None):
        fetched.update(api_base=api_base, owner_repo=owner_repo, number=number)
        return {"number": 1, "title": "wrapper", "body": "make gh-runner", "url": "u"}

    monkeypatch.setattr("issue_runner.cli.fetch_gitea_issue", fake_fetch)
    plan = json.dumps(
        {
            "summary": "s",
            "tickets": [{"title": "t1", "description": "d", "test_assertion": "a == 1"}],
        }
    )
    fake = make_fake_copilot(tmp_path, plan)

    rc = main(
        ["1", "--dir", str(repo), "--copilot-cmd", str(fake), "--plan-only", "--no-github-tickets"]
    )
    assert rc == 0
    assert fetched == {
        "api_base": "http://gitea.local:3000",
        "owner_repo": "Org/thing",
        "number": "1",
    }
    assert "t1" in capsys.readouterr().out


def test_tracker_error_is_friendly_not_traceback(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)  # no origin remote
    rc = main(["1", "--dir", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "origin" in err and "Traceback" not in err


def test_gitea_origin_wires_mirror_backend(tmp_path, monkeypatch):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", "http://gitea.local:3000/Org/thing.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    built = {}

    class FakeBackend:
        def __init__(self, api_base, owner_repo):
            built.update(api_base=api_base, owner_repo=owner_repo)

        def create(self, parent_number, ticket):
            built.setdefault("created", []).append(ticket.id)
            return 500 + ticket.id

        def close(self, number, comment):
            pass

    monkeypatch.setattr("issue_runner.cli.GiteaTickets", FakeBackend)
    monkeypatch.setattr(
        "issue_runner.cli.fetch_gitea_issue",
        lambda *a, **k: {"number": 1, "title": "wrapper", "body": "b", "url": "u"},
    )
    plan = json.dumps(
        {
            "summary": "s",
            "tickets": [{"title": "t1", "description": "d", "test_assertion": "a == 1"}],
        }
    )
    fake = make_fake_copilot(tmp_path, plan)

    rc = main(["1", "--dir", str(repo), "--copilot-cmd", str(fake), "--plan-only"])
    assert rc == 0
    assert built["api_base"] == "http://gitea.local:3000"
    assert built["owner_repo"] == "Org/thing"
    assert built["created"] == [1]


def test_no_pr_flag_disables_pull_request(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    args = build_parser().parse_args(["1", "--dir", str(repo), "--no-pr"])
    assert args.no_pr is True


def test_open_pr_defaults_true_and_reads_config(tmp_path):
    from issue_runner.config import RunnerConfig, load_config

    assert RunnerConfig(repo_dir=tmp_path).open_pr is True
    (tmp_path / "runner.toml").write_text("open_pr = false\n")
    assert load_config(tmp_path).open_pr is False


def test_budget_stop_exits_with_code_4(tmp_path, monkeypatch, capsys):
    """A budget stop must be distinguishable from ordinary blocked tickets (3)."""
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")

    report = RunReport(branch="issue-1-t", done=1, blocked=1, budget_exhausted=True)
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: report)

    rc = cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--max-run-credits",
            "60",
        ]
    )
    assert rc == 4
    assert "budget exhausted" in capsys.readouterr().out


def test_blocked_without_budget_stop_still_exits_3(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: RunReport(done=0, blocked=1))
    rc = cli.main(["--issue-file", str(issue_file), "--dir", str(repo), "--no-github-tickets"])
    assert rc == 3


def test_max_run_credits_flag_reaches_config(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    seen = {}

    def capture(cfg, client, issue, plan_only=False):
        seen["max_run_credits"] = cfg.max_run_credits
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--max-run-credits",
            "120",
        ]
    )
    assert seen["max_run_credits"] == 120


def test_model_and_effort_flags_override_runner_toml_roles(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    (repo / "runner.toml").write_text('[roles.resolver]\nmodel = "cheap"\neffort = "low"\n')
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    seen = {}

    def capture(cfg, client, issue, plan_only=False):
        seen["client"] = client
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--model",
            "strong",
            "--effort",
            "high",
        ]
    )
    argv = seen["client"]._build_argv("p", role="resolver", read_only=False, session_name="resolver")
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == (
        "strong",
        "high",
    )


def test_runner_toml_roles_kept_without_model_or_effort_flags(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    (repo / "runner.toml").write_text('[roles.resolver]\nmodel = "cheap"\neffort = "low"\n')
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    seen = {}

    def capture(cfg, client, issue, plan_only=False):
        seen["client"] = client
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    cli.main(["--issue-file", str(issue_file), "--dir", str(repo), "--no-github-tickets"])
    argv = seen["client"]._build_argv("p", role="resolver", read_only=False, session_name="resolver")
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("cheap", "low")


def test_role_model_flag_overrides_one_role_over_model_flag(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    seen = {}

    def capture(cfg, client, issue, plan_only=False):
        seen["cfg"] = cfg
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--model",
            "base",
            "--role-model",
            "resolver=strong",
        ]
    )
    cfg = seen["cfg"]
    assert {r: cfg.role(r).model for r in ("resolver", "planner")} == {
        "resolver": "strong",
        "planner": "base",
    }


def test_unknown_role_model_role_is_an_invocation_error(tmp_path, monkeypatch):
    from issue_runner import cli

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")

    def must_not_run(*a, **k):
        pytest.fail("must not run")

    monkeypatch.setattr(cli, "run_issue", must_not_run)
    rc = cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--role-model",
            "nosuch=x",
        ]
    )
    assert rc == 2


def test_usage_summary_and_file_from_an_end_to_end_run(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
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
    assert "usage —" in capsys.readouterr().out

    usage_files = list((repo / ".issue-runner").glob("usage-issue-*.json"))
    assert len(usage_files) == 1
    data = json.loads(usage_files[0].read_text())
    assert data["totals"]["calls"] == 1
    assert (repo / ".issue-runner" / "usage.log").exists()

    # the usage file must not be mistaken for a ticket state file
    state_files = list((repo / ".issue-runner").glob("issue-*.json"))
    assert len(state_files) == 1


def test_usage_log_records_the_model_copilot_reported_not_the_requested_one(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
    )
    message = json.dumps({"type": "assistant.message", "data": {"content": plan}})
    call = json.dumps(
        {"type": "model.model_call_success", "data": {"modelCall": {"model": "strong-resolved"}}}
    )
    fake = tmp_path / "fake-copilot"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{message}\n{call}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

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
            "--role-model",
            "planner=strong",
        ]
    )
    assert rc == 0

    last = json.loads((repo / ".issue-runner" / "usage.log").read_text().splitlines()[-1])
    assert last["by_role"]["planner"]["models"] == ["strong-resolved"]


def test_usage_summary_line_shows_the_model_copilot_reported_per_role(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
    )
    message = json.dumps({"type": "assistant.message", "data": {"content": plan}})
    call = json.dumps(
        {"type": "model.model_call_success", "data": {"modelCall": {"model": "strong-resolved"}}}
    )
    fake = tmp_path / "fake-copilot"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{message}\n{call}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

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
            "--role-model",
            "planner=strong",
        ]
    )
    assert rc == 0

    assert "planner=1 (strong-resolved)" in capsys.readouterr().out


def test_usage_falls_back_to_the_role_model_override_when_copilot_reports_no_model(
    tmp_path, capsys
):
    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# Add subtract\n\nNeed a subtract function.")
    plan = json.dumps(
        {
            "summary": "one ticket",
            "tickets": [
                {"title": "subtract ints", "description": "d", "test_assertion": "sub(5,3)==2"}
            ],
        }
    )
    message = json.dumps({"type": "assistant.message", "data": {"content": plan}})
    call = json.dumps({"type": "model.model_call_success", "data": {}})
    fake = tmp_path / "fake-copilot"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{message}\n{call}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

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
            "--model",
            "base",
            "--role-model",
            "planner=strong",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    last_usage_log_line = (repo / ".issue-runner" / "usage.log").read_text().splitlines()[-1]

    assert (
        "planner=1 (strong)" in out,
        json.loads(last_usage_log_line)["by_role"]["planner"]["models"],
    ) == (True, ["strong"])


def test_parser_prog_follows_the_invoked_command_name():
    """`gh-runner --help` must not tell the user to type `issue-runner`."""
    assert build_parser(prog="gh-runner").prog == "gh-runner"
    assert build_parser(prog="issue-runner").prog == "issue-runner"


def test_gh_main_passes_arguments_through(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("# T\n\nbody")
    seen = {}

    def capture(cfg, client, issue, plan_only=False):
        seen["dir"] = cfg.repo_dir
        seen["max_rounds"] = cfg.max_rounds
        seen["plan_only"] = plan_only
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    rc = cli.gh_main(
        [
            "--issue-file",
            str(issue_file),
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--max-rounds",
            "9",
        ]
    )
    assert rc == 0
    assert seen["dir"] == repo.resolve()
    assert seen["max_rounds"] == 9


def test_regression_and_in_place_flags_reach_config(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    issue = tmp_path / "issue.md"
    issue.write_text("# Example\n\nBody")
    seen = {}

    def capture(cfg, *args, **kwargs):
        seen.update(regression=cfg.regression_cmd, isolated=cfg.isolate_worktree)
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    assert (
        cli.main(
            [
                "--issue-file",
                str(issue),
                "--dir",
                str(tmp_path),
                "--regression-cmd",
                "python -m pytest",
                "--in-place",
            ]
        )
        == 0
    )
    assert seen == {"regression": "python -m pytest", "isolated": False}


def test_invalid_budget_is_a_friendly_invocation_error(tmp_path, capsys):
    issue = tmp_path / "issue.md"
    issue.write_text("# Example\n\nBody")
    assert (
        main(
            [
                "--issue-file",
                str(issue),
                "--dir",
                str(tmp_path),
                "--max-run-credits",
                "0",
            ]
        )
        == 2
    )
    assert "positive integer" in capsys.readouterr().err


def test_an_aborted_run_prints_the_partial_summary_not_just_the_error(capsys, monkeypatch):
    """Regression: an aborted run printed one bare error line, so the user could
    not see the branch, the worktree, or the tickets already committed."""
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    report = RunReport(branch="issue-64-demo", worktree="/tmp/wt/64", done=1)
    report.details.append("ticket 1 done @ abc123def456: Parse comma-separated integers")

    def boom(*args, **kwargs):
        error = AttributeError("'list' object has no attribute 'get'")
        error.report = report
        raise error

    monkeypatch.setattr(cli, "run_issue", boom)
    rc = cli._execute(_StubCfg(), _StubClient(), {"number": 64}, False, "gh-runner 64 --visual")
    streams = capsys.readouterr()
    captured = streams.out + streams.err
    assert rc == 1
    assert "issue-64-demo" in captured
    assert "/tmp/wt/64" in captured
    assert "tickets done: 1" in captured
    assert "Parse comma-separated integers" in captured
    assert "AttributeError" in captured, "name the fault type, do not hide it"


class _StubCfg:
    visual = False
    events = None


class _StubUsage:
    calls = 0


class _StubClient:
    usage = _StubUsage()


def _agent_repo(tmp_path, monkeypatch):
    from issue_runner import cli

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    monkeypatch.setattr(
        cli, "fetch_issue", lambda ref, repo=None: {"number": 7, "title": "T", "body": "b"}
    )
    posted = []
    monkeypatch.setattr(
        cli, "comment_issue", lambda repo, number, body: posted.append((repo, number, body))
    )
    return repo, posted


def test_agent_mode_posts_the_summary_instead_of_printing_it(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, posted = _agent_repo(tmp_path, monkeypatch)
    report = RunReport(
        branch="issue-7-t", done=1, pr_url="https://github.com/o/n/pull/9", details=["d1"]
    )
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: report)

    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "branch:" not in out and "pull request" not in out
    assert len(posted) == 1
    target_repo, number, body = posted[0]
    assert (target_repo, number) == ("o/n", 7)
    assert "branch: issue-7-t" in body
    assert "pull request: https://github.com/o/n/pull/9" in body
    assert "  - d1" in body


def test_agent_mode_falls_back_to_printing_when_the_post_fails(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.github_io import GithubError
    from issue_runner.orchestrator import RunReport

    repo, _ = _agent_repo(tmp_path, monkeypatch)

    def refuse(repo, number, body):
        raise GithubError("gh issue comment failed: offline")

    monkeypatch.setattr(cli, "comment_issue", refuse)
    monkeypatch.setattr(
        cli, "run_issue", lambda *a, **k: RunReport(branch="issue-7-t", done=0, blocked=1)
    )

    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent"])

    assert rc == 3
    captured = capsys.readouterr()
    assert "branch: issue-7-t" in captured.out
    assert "could not post the run summary" in captured.err


def test_agent_mode_posts_an_aborted_run_with_its_error(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport
    from issue_runner.phases.build import BuildError

    repo, posted = _agent_repo(tmp_path, monkeypatch)

    def abort(*a, **k):
        error = BuildError("tests exploded")
        error.report = RunReport(branch="issue-7-t", worktree="/w", done=1)
        raise error

    monkeypatch.setattr(cli, "run_issue", abort)

    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "error: tests exploded" in captured.err
    assert "branch:" not in captured.out and "branch:" not in captured.err
    body = posted[0][2]
    assert "error: tests exploded" in body
    assert "branch: issue-7-t" in body and "worktree: /w" in body


def test_agent_mode_posts_to_a_gitea_issue(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", "http://gitea.local:3000/Org/thing.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    posted = []

    class FakeGitea:
        def __init__(self, api_base, owner_repo):
            self.where = (api_base, owner_repo)

        def comment_issue(self, number, body):
            posted.append((self.where, number, body))

    monkeypatch.setattr(cli, "GiteaTickets", FakeGitea)
    monkeypatch.setattr(
        cli, "fetch_gitea_issue", lambda *a, **k: {"number": 3, "title": "T", "body": "b"}
    )
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: RunReport(branch="issue-3-t", done=1))

    rc = cli.main(["3", "--dir", str(repo), "--no-github-tickets", "--agent"])

    assert rc == 0
    assert "branch:" not in capsys.readouterr().out
    assert posted[0][:2] == (("http://gitea.local:3000", "Org/thing"), 3)
    assert "branch: issue-3-t" in posted[0][2]


def test_agent_mode_rejects_an_issue_file_without_an_issue_to_comment_on(
    tmp_path, monkeypatch, capsys
):
    from issue_runner import cli

    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: pytest.fail("must not run"))
    rc = cli.main(["--issue-file", "issue.md", "--dir", str(tmp_path), "--agent"])
    assert rc == 2
    assert "--comment-issue" in capsys.readouterr().err


def test_comment_issue_needs_agent_mode(tmp_path, monkeypatch, capsys):
    from issue_runner import cli

    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: pytest.fail("must not run"))
    rc = cli.main(["7", "--dir", str(tmp_path), "--comment-issue", "7"])
    assert rc == 2
    assert "--agent" in capsys.readouterr().err


def test_agent_mode_keeps_an_explicit_visual_and_still_posts(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, posted = _agent_repo(tmp_path, monkeypatch)
    seen = {}

    def capture(cfg, *a, **k):
        seen["visual"] = cfg.visual
        return RunReport(branch="issue-7-t", done=1)

    monkeypatch.setattr(cli, "run_issue", capture)
    rc = cli.main(
        ["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent", "--visual"]
    )
    assert rc == 0
    assert seen["visual"] is True
    assert [(r, n) for r, n, _ in posted] == [("o/n", 7)]
    assert "branch:" not in capsys.readouterr().out


def test_agent_mode_posts_an_issue_file_run_to_the_comment_issue(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, posted = _agent_repo(tmp_path, monkeypatch)
    issue_file = tmp_path / "review.md"
    issue_file.write_text("# r1-pr9-review\n\nfix the review findings\n")
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: RunReport(branch="issue-r1", done=1))

    rc = cli.main(
        [
            "--issue-file",
            str(issue_file),
            "--repo",
            "o/n",
            "--dir",
            str(repo),
            "--no-github-tickets",
            "--no-pr",
            "--agent",
            "--comment-issue",
            "7",
        ]
    )
    assert rc == 0
    assert [(r, n) for r, n, _ in posted] == [("o/n", 7)]
    assert "branch: issue-r1" in posted[0][2]


def test_agent_mode_finds_the_github_repo_of_an_issue_file_run_from_origin(
    tmp_path, monkeypatch, capsys
):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, posted = _agent_repo(tmp_path, monkeypatch)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/o/n.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    issue_file = tmp_path / "review.md"
    issue_file.write_text("# r1\n\nb\n")
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: RunReport(done=1))

    rc = cli.main(
        ["--issue-file", str(issue_file), "--dir", str(repo), "--no-github-tickets"]
        + ["--agent", "--comment-issue", "9"]
    )
    assert rc == 0, capsys.readouterr().err
    assert [(r, n) for r, n, _ in posted] == [("o/n", 9)]


def test_agent_mode_without_an_origin_is_a_clean_error(tmp_path, monkeypatch, capsys):
    from issue_runner import cli

    repo, _ = _agent_repo(tmp_path, monkeypatch)
    issue_file = tmp_path / "review.md"
    issue_file.write_text("# r1\n\nb\n")
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: pytest.fail("must not run"))

    rc = cli.main(
        ["--issue-file", str(issue_file), "--dir", str(repo), "--no-github-tickets"]
        + ["--agent", "--comment-issue", "9"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot comment" in err and "Traceback" not in err


def test_agent_mode_turns_off_a_configured_visual(tmp_path, monkeypatch):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, _ = _agent_repo(tmp_path, monkeypatch)
    (repo / "runner.toml").write_text("visual = true\n")
    seen = {}

    def capture(cfg, *a, **k):
        seen["visual"] = cfg.visual
        return RunReport()

    monkeypatch.setattr(cli, "run_issue", capture)
    cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent"])
    assert seen["visual"] is False


def test_agent_mode_visual_abort_posts_the_error(tmp_path, monkeypatch, capsys):
    from issue_runner import cli, visual_display

    repo, posted = _agent_repo(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        visual_display,
        "run_visual",
        lambda *a, **k: (None, RuntimeError("planner blew up"), False),
    )
    rc = cli.main(
        ["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--agent", "--visual"]
    )
    assert rc == 1
    assert "error: planner blew up" in capsys.readouterr().err
    [(_, number, body)] = posted
    assert number == 7 and "error: planner blew up" in body


DEPLOY_ISSUE = {
    "number": 7,
    "title": "T",
    "body": "## Acceptance criteria\n\n- [ ] it works\n",
}


def _deploy_repo(tmp_path, monkeypatch, fake=None):
    from issue_runner import cli
    from issue_runner.github_flow import GitHubFlow
    from tests.fake_github import FakeGitHub

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    monkeypatch.setattr(cli, "fetch_issue", lambda ref, repo=None: dict(DEPLOY_ISSUE))
    fake = fake or FakeGitHub()
    monkeypatch.setattr(cli, "GitHubFlow", lambda name: GitHubFlow(name, run=fake))
    return repo, fake


@pytest.mark.parametrize(
    "extra, named",
    [
        (["7", "--no-pr"], "--no-pr"),
        (["7", "--plan-only"], "--plan-only"),
        (["--issue-file", "i.md"], "--issue-file"),
        (["--demo"], "--demo"),
        (["7", "--in-place"], "--in-place"),
    ],
)
def test_deploy_rejects_flags_that_cannot_reach_dev(tmp_path, monkeypatch, capsys, extra, named):
    from issue_runner import cli

    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: pytest.fail("must not run"))
    rc = cli.main([*extra, "--dir", str(tmp_path), "--deploy"])
    assert rc == 2
    assert f"--deploy cannot be used with {named}" in capsys.readouterr().err


def test_deploy_needs_a_github_repository(tmp_path, monkeypatch, capsys):
    from issue_runner import cli

    repo = tmp_path / "target"
    repo.mkdir()
    git_init(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", "http://gitea.local:3000/Org/thing.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(cli, "fetch_gitea_issue", lambda *a, **k: dict(DEPLOY_ISSUE))
    monkeypatch.setattr(cli, "run_issue", lambda *a, **k: pytest.fail("must not run"))
    rc = cli.main(["7", "--dir", str(repo), "--no-github-tickets", "--deploy"])
    assert rc == 2
    assert "needs a GitHub repository" in capsys.readouterr().err


def test_deploy_preflight_failure_stops_before_any_model_call(tmp_path, monkeypatch, capsys):
    from issue_runner import cli

    repo, fake = _deploy_repo(tmp_path, monkeypatch)
    fake.environments.clear()
    monkeypatch.setattr(cli, "CopilotClient", lambda cfg: pytest.fail("no client before preflight"))
    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--deploy"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "preflight failed" in err and "environment 'development' not found" in err


def test_deploy_dry_run_prints_the_preflight(tmp_path, monkeypatch, capsys):
    repo, _ = _deploy_repo(tmp_path, monkeypatch)
    rc = main(
        ["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--deploy", "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "deploy preflight passed for o/n" in out
    assert "AC1: it works" in out
    assert not (repo / ".issue-runner").exists()


@pytest.mark.parametrize(
    "outcome, code",
    [({"dod_met": True}, 0), ({"dod_failed_gate": "review"}, 6), ({"blocked": 1}, 3)],
)
def test_deploy_exit_code_follows_the_definition_of_done(tmp_path, monkeypatch, outcome, code):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, _ = _deploy_repo(tmp_path, monkeypatch)
    seen = {}

    def capture(cfg, *a, **k):
        seen.update(deploy=cfg.deploy, open_pr=cfg.open_pr, preflight=cfg.preflight)
        return RunReport(deploy=True, **outcome)

    monkeypatch.setattr(cli, "run_issue", capture)
    (repo / "runner.toml").write_text("open_pr = false\n")
    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--deploy"])
    assert rc == code
    assert seen["deploy"] is True and seen["open_pr"] is True
    assert seen["preflight"].merge_method == "squash"


def test_deploy_settings_load_from_runner_toml(tmp_path):
    from issue_runner.config import ConfigError, load_config, validate_config

    (tmp_path / "runner.toml").write_text(
        "[deploy]\nworkflow = 'deploy.yml'\nmax_review_rounds = 5\nmerge_method = 'squash'\n"
    )
    cfg = load_config(tmp_path)
    validate_config(cfg)
    assert cfg.deploy_settings.workflow == "deploy.yml"
    assert cfg.deploy_settings.max_review_rounds == 5
    assert cfg.deploy_settings.environment == "development"

    for bad, message in (
        ("[deploy]\nreview_bots = 'x'\n", "unknown"),
        ("[deploy]\nmerge_method = 'yolo'\n", "merge_method"),
        ("[deploy]\npoll_seconds = 0\n", "poll_seconds"),
        ("[deploy]\nworkflow = ''\n", "workflow"),
        ("deploy = 1\n", "must be a table"),
    ):
        (tmp_path / "runner.toml").write_text(bad)
        with pytest.raises(ConfigError, match=message):
            validate_config(load_config(tmp_path))


def test_deploy_summary_prints_the_definition_of_done(tmp_path, monkeypatch, capsys):
    from issue_runner import cli
    from issue_runner.orchestrator import RunReport

    repo, _ = _deploy_repo(tmp_path, monkeypatch)
    gates = {"tickets": "pass", "criteria_tests": "pass", "review": "pass", "merge": "fail"}
    monkeypatch.setattr(
        cli,
        "run_issue",
        lambda *a, **k: RunReport(deploy=True, done=1, dod_failed_gate="merge", gates=gates),
    )
    rc = cli.main(["7", "--repo", "o/n", "--dir", str(repo), "--no-github-tickets", "--deploy"])
    out = capsys.readouterr().out
    assert rc == 7
    assert "definition of done: FAILED at merge" in out
    assert "  review          pass\n" in out
    assert "  merge           FAILED\n" in out
    assert "  criteria_dev    not reached\n" in out
