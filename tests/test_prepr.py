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
