import json
import stat
import subprocess

from issue_runner.cli import main


def make_fake_copilot(tmp_path, reply):
    """A stand-in copilot binary: ignores args, prints a canned reply."""
    fake = tmp_path / "fake-copilot"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{reply}\nEOF\n")
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


def test_requires_issue_ref_or_file(tmp_path, capsys):
    rc = main(["--dir", str(tmp_path)])
    assert rc == 2
