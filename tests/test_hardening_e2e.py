import json
import os
import subprocess
import sys
from pathlib import Path

from issue_runner.demo import setup_demo
from tests.hardening_support import git

PROJECT = Path(__file__).resolve().parents[1]


def cli(*args):
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(PROJECT / "src"), str(PROJECT)]),
        PYTHONDONTWRITEBYTECODE="1",
        ISSUE_RUNNER_DEMO_DELAY="0",
    )
    return subprocess.run(
        [sys.executable, "-m", "issue_runner.cli", *map(str, args)],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def test_real_cli_exposes_regression_and_workspace_controls():
    result = cli("--help")
    assert result.returncode == 0
    assert "--regression-cmd" in result.stdout
    assert "--in-place" in result.stdout


def test_real_cli_demo_persists_work_and_headless_usage(tmp_path):
    repo = tmp_path / "demo"
    result = cli("--demo", "--demo-dir", repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tickets done: 2, blocked: 0" in result.stdout
    assert f"worktree: {repo}" in result.stdout
    assert git(repo, "status", "--porcelain") == ""
    proof = subprocess.run(
        [sys.executable, "-c", "from demo_pkg.stats import median; print(median([1,2,3,4]))"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proof.stdout.strip() == "2.5"
    usage = json.loads(next((repo / ".issue-runner").glob("usage-issue-*.json")).read_text())
    assert usage["totals"]["input_tokens"] > 0
    assert usage["totals"]["nano_aiu"] == 0
    assert ".issue-runner/" not in git(repo, "ls-files")


def test_real_cli_refuses_to_reset_an_unowned_directory(tmp_path):
    target = tmp_path / "ordinary-project"
    target.mkdir()
    note = target / "keep.txt"
    note.write_text("user data\n")
    result = cli("--demo", "--demo-dir", target, "--demo-reset")
    assert result.returncode == 2
    assert note.read_text() == "user data\n"
    assert list(target.iterdir()) == [note]
    assert "Traceback" not in result.stderr


def test_real_cli_reports_a_missing_copilot_binary_without_a_traceback(tmp_path):
    env = setup_demo(tmp_path / "source")
    result = cli(
        "--issue-file",
        env.issue_file,
        "--dir",
        env.repo_dir,
        "--copilot-cmd",
        tmp_path / "missing-binary",
        "--no-pr",
        "--no-github-tickets",
    )
    assert result.returncode == 1
    assert "could not start copilot" in result.stderr
    assert "Traceback" not in result.stderr


def test_real_cli_budget_resume_skips_accepted_paid_phases(tmp_path):
    env = setup_demo(tmp_path / "source")
    fake = tmp_path / "metered-copilot"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from issue_runner.demo.responder import parse_argv, respond, role_of\n"
        "prompt, root, stream = parse_argv(sys.argv[1:])\n"
        "role = role_of(prompt)\n"
        "reply, edited, reasoning = respond(prompt, root)\n"
        "if role == 'planner':\n"
        "    plan = json.loads(reply)\n"
        "    plan['tickets'] = plan['tickets'][:1]\n"
        "    reply = json.dumps(plan)\n"
        "trace = root / '.issue-runner' / 'metered-calls.jsonl'\n"
        "with trace.open('a') as log:\n"
        "    log.write(json.dumps(role) + '\\n')\n"
        "print(json.dumps({'type': 'model.model_call_success', 'data': {\n"
        "    'responseUsage': {'prompt_tokens': 5, 'completion_tokens': 2},\n"
        "    'copilotUsage': {'total_nano_aiu': 1000000000}}}))\n"
        "print(json.dumps({'type': 'assistant.message', 'data': {'content': reply}}))\n"
    )
    fake.chmod(0o755)
    args = [
        "--issue-file",
        env.issue_file,
        "--dir",
        env.repo_dir,
        "--copilot-cmd",
        fake,
        "--test-cmd",
        env.test_cmd,
        "--regression-cmd",
        env.test_cmd.format(test_path="tests"),
        "--no-pr",
        "--no-github-tickets",
        "--max-run-credits",
    ]
    stopped = cli(*args, "2")
    assert stopped.returncode == 4, stopped.stdout + stopped.stderr
    state_file = next((env.repo_dir / ".issue-runner").glob("issue-*.json"))
    state = json.loads(state_file.read_text())
    workspace = Path(state["worktree"])
    assert state["tickets"][0]["phase"] == "coder"
    assert state["tickets"][0]["status"] == "pending"
    trace = workspace / ".issue-runner" / "metered-calls.jsonl"
    assert [json.loads(line) for line in trace.read_text().splitlines()] == ["planner", "tester"]

    resumed = cli(*args, "30")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "tickets done: 1, blocked: 0" in resumed.stdout
    assert [json.loads(line) for line in trace.read_text().splitlines()] == [
        "planner",
        "tester",
        "coder",
        "verifier",
    ]
    state = json.loads(state_file.read_text())
    assert state["tickets"][0]["commit_sha"] == git(workspace, "rev-parse", "HEAD")
    assert git(env.repo_dir, "branch", "--show-current") == "main"
    assert git(env.repo_dir, "status", "--porcelain") == ""
    usage_file = next((env.repo_dir / ".issue-runner").glob("usage-issue-*.json"))
    usage = json.loads(usage_file.read_text())
    assert usage["totals"]["calls"] == 4
    assert usage["totals"]["nano_aiu"] == 4_000_000_000
