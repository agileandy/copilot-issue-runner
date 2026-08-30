import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

from issue_runner.cli import gh_main, main


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
