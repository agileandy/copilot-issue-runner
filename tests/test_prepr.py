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


def test_run_checks_refuses_when_command_moves_head(tmp_path):
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
    cfg.pre_pr.commands = ["git commit --allow-empty -m sneaky"]

    with pytest.raises(DevopsError, match=r"pre-PR command.*moved the run branch"):
        run_checks(cfg, "base")


def test_run_checks_refuses_when_command_cannot_start(tmp_path):
    import pytest

    from issue_runner.phases.devops import DevopsError
    from issue_runner.phases.prepr import run_checks

    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = ["/nonexistent-pre-pr-tool-xyz --base {base}"]

    with pytest.raises(DevopsError, match=r"pre-PR command.*could not run"):
        run_checks(cfg, "abc")


def test_run_checks_raises_tool_error_when_command_exits_2(tmp_path):
    import pytest

    from issue_runner.phases.devops import DevopsError
    from issue_runner.phases.prepr import run_checks

    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = [f"{sys.executable} -c 'import sys; print(\"boom\"); sys.exit(2)'"]

    with pytest.raises(DevopsError, match=r"pre-PR command.*exit 2"):
        run_checks(cfg, "abc")


def test_step_records_every_line_of_a_finding_in_report_details(tmp_path):
    from issue_runner.orchestrator import RunReport
    from issue_runner.phases import prepr
    from issue_runner.tickets import TicketStore

    cmd = (
        f"{sys.executable} -c "
        "'print(\"a.py:1 x\"); print(\"b.py:2 y\"); print(\"c.py:3 z\"); "
        "import sys; sys.exit(1)'"
    )
    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = [cmd]
    store = TicketStore(tmp_path, "1")
    report = RunReport()

    prepr.step(cfg, store, report)

    assert {f"  {cmd}: a.py:1 x", "    b.py:2 y", "    c.py:3 z"} <= set(report.details)


def test_step_persists_findings_as_pre_pr_tickets_with_their_command(tmp_path):
    from issue_runner.phases import prepr
    from issue_runner.tickets import TicketStore

    cmd = f"{sys.executable} -c 'print(\"unused import\"); import sys; sys.exit(1)'"
    cfg = RunnerConfig(repo_dir=tmp_path)
    cfg.pre_pr.commands = [cmd]
    store = TicketStore(tmp_path, "1")

    prepr.step(cfg, store)

    reloaded = TicketStore(tmp_path, "1")
    reloaded.load()
    t = reloaded.tickets[0]
    assert (t.kind, t.pre_pr_command) == ("pre_pr", cmd)


def test_finding_for_returns_finding_until_fixed_then_none(tmp_path, tmp_path_factory):
    from issue_runner.phases import prepr

    script = tmp_path_factory.mktemp("lint-tool") / "fake_lint.py"
    script.write_text(
        "import pathlib, sys\n"
        f"if pathlib.Path({str(tmp_path / 'fixed')!r}).exists():\n"
        "    sys.exit(0)\n"
        "print('src/app.py:1: unused import')\n"
        "sys.exit(1)\n"
    )
    cfg = RunnerConfig(repo_dir=tmp_path)
    cmd = f"{sys.executable} {script}"

    assert (
        "unused import" in prepr.finding_for(cfg, cmd),
        (tmp_path / "fixed").touch() or prepr.finding_for(cfg, cmd),
    ) == (True, None)
