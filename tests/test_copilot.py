import subprocess

import pytest

from issue_runner.config import RoleConfig, RunnerConfig
from issue_runner.copilot import CopilotClient, CopilotError


class RecordingRunner:
    def __init__(self, stdout="ok", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def make_client(tmp_path, runner, **cfg_kwargs):
    cfg = RunnerConfig(repo_dir=tmp_path, **cfg_kwargs)
    return CopilotClient(cfg, runner=runner)


def test_builds_non_interactive_argv(tmp_path):
    runner = RecordingRunner()
    client = make_client(tmp_path, runner)
    client.run("do a thing", role="builder.coder")

    argv, _kwargs = runner.calls[0]
    assert argv[0] == "copilot"
    assert "-p" in argv and "do a thing" in argv
    for flag in ("-s", "--allow-all-tools", "--no-ask-user", "--no-auto-update"):
        assert flag in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    # git push must always be denied
    assert "shell(git push)" in argv[argv.index("--deny-tool") + 1 :]


def test_role_model_and_effort_applied(tmp_path):
    runner = RecordingRunner()
    client = make_client(
        tmp_path, runner, roles={"verifier": RoleConfig(model="gpt-5-mini", effort="low")}
    )
    client.run("check", role="verifier")

    argv, _ = runner.calls[0]
    assert argv[argv.index("--model") + 1] == "gpt-5-mini"
    assert argv[argv.index("--effort") + 1] == "low"


def test_read_only_roles_deny_writes(tmp_path):
    runner = RecordingRunner()
    client = make_client(tmp_path, runner)
    client.run("review", role="verifier", read_only=True)

    argv, _ = runner.calls[0]
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny-tool"]
    assert "write" in denied
    assert "shell(git:*)" in denied


def test_returns_stripped_stdout(tmp_path):
    runner = RecordingRunner(stdout="  the answer \n")
    client = make_client(tmp_path, runner)
    assert client.run("q", role="planner") == "the answer"


def test_nonzero_exit_raises(tmp_path):
    runner = RecordingRunner(returncode=1)
    client = make_client(tmp_path, runner)
    with pytest.raises(CopilotError):
        client.run("q", role="planner")


def test_custom_copilot_cmd(tmp_path):
    runner = RecordingRunner()
    client = make_client(tmp_path, runner, copilot_cmd="/fake/copilot")
    client.run("q", role="planner")
    assert runner.calls[0][0][0] == "/fake/copilot"


def test_timeout_raises_copilot_error(tmp_path):
    def hanging_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    client = make_client(tmp_path, hanging_runner)
    with pytest.raises(CopilotError, match="timed out"):
        client.run("q", role="planner")


def test_visual_mode_never_touches_copilot_argv(tmp_path):
    runner = RecordingRunner()
    cfg = RunnerConfig(repo_dir=tmp_path, visual=True)
    client = CopilotClient(cfg, runner=runner)
    client.run("q", role="planner")
    argv, _ = runner.calls[0]
    assert "--visual" not in argv  # copilot has no such flag; visual is runner-side only
    assert argv[argv.index("-C") + 1] == str(tmp_path)  # -C keeps its directory value
