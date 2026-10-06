"""Run worktrees work without permission walls.

Pins the Copilot argv for builder and verifier runs, the worktree toolchain
provisioning, the workspace rules every agent prompt carries, and the
feature/ and bugfix/ run branch names (with legacy issue-* runs still resuming).
"""

import json
import subprocess
from pathlib import Path

import pytest

from issue_runner import toolchain
from issue_runner.config import RunnerConfig
from issue_runner.copilot import ALWAYS_DENY, MUTATING_GIT, READ_ONLY_DENY, CopilotClient
from issue_runner.orchestrator import _prepare_toolchain, run_issue
from issue_runner.tickets import TicketStore
from tests.hardening_support import ISSUE, DemoClient, Interrupted, git, load_store, sandbox

MUTATING = (
    "commit",
    "push",
    "reset",
    "checkout",
    "switch",
    "stash",
    "clean",
    "rebase",
    "merge",
    "cherry-pick",
    "tag",
    "branch",
    "worktree",
    "config",
)


def _argv(tmp_path, role, read_only):
    client = CopilotClient(RunnerConfig(repo_dir=tmp_path))
    return client._build_argv("PROMPT", role, read_only, f"{role}-t1", structured=True)


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


# --- copilot argv ------------------------------------------------------------


def test_read_only_deny_keeps_write_and_names_only_mutating_git():
    assert READ_ONLY_DENY[0] == "write"
    assert "shell(git:*)" not in READ_ONLY_DENY
    for verb in MUTATING:
        assert f"shell(git {verb})" in READ_ONLY_DENY
    for verb in ("status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep"):
        assert f"shell(git {verb})" not in READ_ONLY_DENY


def test_builder_argv_is_pinned(tmp_path):
    argv = _argv(tmp_path, "builder.coder", read_only=False)
    print("builder argv:", argv)
    assert argv == [
        "copilot",
        "-p",
        "PROMPT",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--no-ask-user",
        "--no-auto-update",
        "--no-color",
        "--log-level",
        "error",
        "-C",
        str(tmp_path),
        "--add-dir",
        str(tmp_path),
        "--deny-tool",
        "shell(git push)",
        "--name",
        "builder.coder-t1",
    ]


def test_verifier_argv_is_pinned(tmp_path):
    argv = _argv(tmp_path, "verifier", read_only=True)
    print("verifier argv:", argv)
    expected_deny = ["shell(git push)", "write"] + [
        f"shell(git {verb})" for verb in MUTATING_GIT if verb != "push"
    ]
    assert _flag_values(argv, "--deny-tool") == expected_deny
    assert argv[: argv.index("--deny-tool")] == [
        "copilot",
        "-p",
        "PROMPT",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--no-ask-user",
        "--no-auto-update",
        "--no-color",
        "--log-level",
        "error",
        "-C",
        str(tmp_path),
        "--add-dir",
        str(tmp_path),
    ]
    assert argv[-2:] == ["--name", "verifier-t1"]


@pytest.mark.parametrize("read_only", [False, True])
def test_path_access_is_the_worktree_and_nothing_broader(tmp_path, read_only):
    argv = _argv(tmp_path, "verifier", read_only)
    assert _flag_values(argv, "--add-dir") == [str(tmp_path)]
    assert _flag_values(argv, "-C") == [str(tmp_path)]
    for broad in ("--allow-all-paths", "--allow-all", "--yolo", "--allow-tool"):
        assert broad not in argv
    # every shell command is already approved; denials still win over it
    assert "--allow-all-tools" in argv
    assert set(ALWAYS_DENY) <= set(_flag_values(argv, "--deny-tool"))


# --- toolchain discovery -------------------------------------------------------


def _ai_maint_layout(root: Path) -> Path:
    (root / "pyproject.toml").write_text("[project]\nname='ai-maint'\n")
    (root / "backend" / "cdk" / "node_modules" / "dep").mkdir(parents=True)
    (root / "backend" / "requirements.txt").write_text("boto3==1.40.7\ncoverage==7.6.12\n")
    (root / "backend" / "requirements-dev.txt").write_text("coverage==7.15.4\n")
    (root / "backend" / "cdk" / "package-lock.json").write_text("{}")
    # a lockfile inside installed dependencies is not a project
    (root / "backend" / "cdk" / "node_modules" / "dep" / "package-lock.json").write_text("{}")
    (root / "frontend").mkdir()
    (root / "frontend" / "package-lock.json").write_text("{}")
    return root


def test_ai_maint_layout_is_discovered(tmp_path):
    root = _ai_maint_layout(tmp_path)
    plans = toolchain.discover(root, [".venv/bin/python -m pytest {test_path} -q"])
    steps = [(s.cwd, s.argv) for plan in plans for s in plan.steps]
    py = ".venv/bin/python"
    assert steps == [
        (".", ("uv", "venv", "--allow-existing", ".venv")),
        (".", ("uv", "pip", "install", "--python", py, "-r", "backend/requirements.txt")),
        (".", ("uv", "pip", "install", "--python", py, "-r", "backend/requirements-dev.txt")),
        (".", ("uv", "pip", "install", "--python", py, "pytest")),
        ("frontend", ("npm", "ci", "--no-audit", "--no-fund", "--prefer-offline")),
        ("backend/cdk", ("npm", "ci", "--no-audit", "--no-fund", "--prefer-offline")),
    ]


def test_uv_lock_project_is_synced(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "requirements.txt").write_text("ignored\n")
    plans = toolchain.discover(tmp_path, ["uv run --no-sync python -m pytest -q"])
    assert [s.argv for p in plans for s in p.steps] == [("uv", "sync", "--frozen")]


def test_pytest_is_not_added_when_requirements_pin_it_or_tests_do_not_use_it(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest==8.0\n")
    pinned = toolchain.discover(tmp_path, ["python -m pytest -q"])
    assert not any("pytest" in s.argv for p in pinned for s in p.steps)
    (tmp_path / "requirements.txt").write_text("boto3\n")
    other = toolchain.discover(tmp_path, ["npm test"])
    assert not any("pytest" in s.argv for p in other for s in p.steps)


def test_nothing_to_provision_without_lockfiles_or_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert toolchain.discover(tmp_path, ["python -m pytest -q"]) == []


# --- provisioning -------------------------------------------------------------


class FakeRun:
    def __init__(self, returncode=0, timeout=False):
        self.calls = []
        self.returncode = returncode
        self.timeout = timeout

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        cwd = Path(kwargs["cwd"])
        if argv[:2] == ["uv", "venv"]:
            python = toolchain.venv_python(cwd)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        if argv[:2] == ["npm", "ci"]:
            (cwd / "node_modules").mkdir(exist_ok=True)
            (cwd / "node_modules" / ".package-lock.json").write_text("{}")
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr="boom")


def test_provision_runs_each_step_in_the_worktree_with_a_timeout(tmp_path, caplog):
    root = _ai_maint_layout(tmp_path)
    run = FakeRun()
    with caplog.at_level("INFO", logger="issue_runner"):
        ran = toolchain.provision(root, ["python -m pytest -q"], timeout=42, run=run)
    assert len(ran) == 6
    for argv, kwargs in run.calls:
        assert kwargs["timeout"] == 42
        assert Path(kwargs["cwd"]).is_relative_to(root)
    assert any("toolchain: npm ci in frontend" in r.message for r in caplog.records)
    assert (root / ".venv" / toolchain.STAMP).is_file()


def test_provision_is_skipped_once_present(tmp_path, caplog):
    root = _ai_maint_layout(tmp_path)
    toolchain.provision(root, ["python -m pytest -q"], timeout=42, run=FakeRun())
    again = FakeRun()
    with caplog.at_level("INFO", logger="issue_runner"):
        assert toolchain.provision(root, ["python -m pytest -q"], timeout=42, run=again) == []
    assert again.calls == []
    assert sum("already provisioned; skipped" in r.message for r in caplog.records) == 3


def test_a_failed_step_stops_provisioning(tmp_path):
    root = _ai_maint_layout(tmp_path)
    with pytest.raises(toolchain.ProvisionError, match="(?s)failed \\(exit 1\\).*boom"):
        toolchain.provision(root, [], timeout=42, run=FakeRun(returncode=1))
    assert not (root / ".venv" / toolchain.STAMP).exists()


def test_a_timed_out_step_names_the_setting(tmp_path):
    root = _ai_maint_layout(tmp_path)
    with pytest.raises(toolchain.ProvisionError, match="timed out after 42s.*provision_timeout"):
        toolchain.provision(root, [], timeout=42, run=FakeRun(timeout=True))


def _git_repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "t@test.local"),
        ("config", "user.name", "T"),
        ("commit", "--allow-empty", "-m", "seed"),
    ):
        git(path, *args)
    return path


def test_prepare_toolchain_provisions_the_worktree_and_retargets_tests(tmp_path, monkeypatch):
    source = _git_repo(tmp_path / "source")
    workspace = tmp_path / "workspace"
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").touch()
    (workspace / "pyproject.toml").write_text("[project]\nname='x'\n")
    calls = []
    monkeypatch.setattr(toolchain, "provision", lambda root, cmds, timeout: calls.append(root))
    cfg = RunnerConfig(
        repo_dir=workspace,
        test_cmd="/elsewhere/source/.venv/bin/python -m pytest {test_path} -q",
        test_cmd_detected=True,
        provision_timeout=7,
    )
    _prepare_toolchain(cfg, source)
    assert calls == [workspace.resolve()]
    assert cfg.test_cmd == ".venv/bin/python -m pytest {test_path} -q"
    exclude = Path(git(source, "rev-parse", "--git-path", "info/exclude"))
    excluded = (exclude if exclude.is_absolute() else source / exclude).read_text().splitlines()
    assert "/.venv/" in excluded and "node_modules/" in excluded


@pytest.mark.parametrize("in_place", [True, False])
def test_prepare_toolchain_respects_in_place_and_opt_out(tmp_path, monkeypatch, in_place):
    source = _git_repo(tmp_path / "source")
    workspace = source if in_place else tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    calls = []
    monkeypatch.setattr(toolchain, "provision", lambda *a: calls.append(a))
    cfg = RunnerConfig(repo_dir=workspace, test_cmd="custom {test_path}", provision=in_place)
    _prepare_toolchain(cfg, source)
    assert calls == []
    assert cfg.test_cmd == "custom {test_path}"


# --- runs: branches and prompts -------------------------------------------------


class PromptClient(DemoClient):
    def __init__(self, cfg, **kwargs):
        super().__init__(cfg, **kwargs)
        self.prompts = {}

    def run(self, prompt, role, **kwargs):
        self.prompts.setdefault(role, prompt)
        return super().run(prompt, role, **kwargs)


def test_isolated_run_uses_a_feature_branch_and_briefs_every_agent(tmp_path):
    env, cfg = sandbox(tmp_path, isolated=True)
    client = PromptClient(cfg)
    report = run_issue(cfg, client, ISSUE)

    assert report.branch == "feature/17-add-statistics"
    assert (report.done, report.blocked) == (1, 0)
    assert git(Path(report.worktree), "branch", "--show-current") == report.branch
    assert set(client.prompts) == {"planner", "builder.tester", "builder.coder", "verifier"}
    for role, prompt in client.prompts.items():
        assert f"Your workspace is {report.worktree}" in prompt, role
        assert "Never call the primary checkout's .venv or node_modules" in prompt, role
        assert "Use uv, never pip." in prompt, role
        assert "If a tool call is denied, try one different approach, then report" in prompt
        assert "never repeat it" in prompt, role
        assert f"Run the full suite with: `{cfg.regression_cmd}`" in prompt, role
    assert "<test_path>" in client.prompts["builder.tester"]
    test_path = load_store(env).tickets[0].test_path
    focused = cfg.test_cmd.format(test_path=test_path)
    for role in ("builder.coder", "verifier"):
        assert f"Run the test for this sub-task with: `{focused}`" in client.prompts[role]
    assert "denied — read the files directly" not in client.prompts["verifier"]
    assert "`git diff HEAD`" in client.prompts["verifier"]


def test_bug_issue_uses_a_bugfix_branch(tmp_path):
    _env, cfg = sandbox(tmp_path, isolated=True)
    issue = dict(ISSUE, labels=[{"name": "bug"}])
    report = run_issue(cfg, DemoClient(cfg), issue)
    assert report.branch == "bugfix/17-add-statistics"


def test_in_flight_legacy_issue_branch_still_resumes_and_commits(tmp_path):
    env, cfg = sandbox(tmp_path, isolated=True)
    with pytest.raises(Interrupted):
        run_issue(cfg, DemoClient(cfg, before="builder.coder"), ISSUE)
    store = load_store(env)
    workspace = Path(store.worktree)
    legacy = "issue-17-add-statistics"
    git(workspace, "branch", "-m", store.branch, legacy)
    state = json.loads(store.state_file.read_text())
    state["branch"] = legacy
    store.state_file.write_text(json.dumps(state))

    report = run_issue(cfg, DemoClient(cfg), ISSUE)
    assert report.branch == legacy
    assert (report.done, report.blocked) == (1, 0)
    assert git(workspace, "branch", "--show-current") == legacy
    assert "feature/17-add-statistics" not in git(env.repo_dir, "branch", "--list")
    assert TicketStore(env.repo_dir / ".issue-runner", issue_ref="17").load()
