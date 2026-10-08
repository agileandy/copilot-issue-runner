import sys

from issue_runner.config import RunnerConfig


def test_run_checks_reports_only_failing_command_with_base_substituted(tmp_path):
    from issue_runner.phases.prepr import run_checks

    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = [
        f"{sys.executable} -c 'import sys; print(\"bad \" + sys.argv[1]); sys.exit(1)' {{base}}",
        f"{sys.executable} -c 'pass'",
    ]

    findings = run_checks(cfg, "abc123")

    assert len(findings) == 1 and "bad abc123" in findings[0]["text"]


def test_run_checks_refuses_when_command_changes_worktree(tmp_path):
    import subprocess

    import pytest

    from issue_runner.phases.devops import DevopsError
    from issue_runner.phases.prepr import run_checks

    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = [f"{sys.executable} -c \"open('stray.txt','w').write('x')\""]

    with pytest.raises(DevopsError, match=r"pre-PR command.*stray\.txt"):
        run_checks(cfg, "base")
