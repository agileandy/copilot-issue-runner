"""The offline fixtures used by automated tests stay safe and dependency-free."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from issue_runner import demo as demo_module
from issue_runner.cli import main
from issue_runner.demo import DemoError, demo_test_cmd, minitest, setup_demo
from issue_runner.demo.responder import respond


def run_offline_fixture(dest):
    env = setup_demo(dest)
    return main(
        [
            "--issue-file",
            str(env.issue_file),
            "--dir",
            str(env.repo_dir),
            "--copilot-cmd",
            str(env.copilot_cmd),
            "--test-cmd",
            env.test_cmd,
            "--regression-cmd",
            env.test_cmd.format(test_path="tests"),
            "--in-place",
            "--no-github-tickets",
            "--no-pr",
        ]
    )


class _RmtreeCalled(AssertionError):
    """Raised by the interception below: a guard let a deletion through."""


@pytest.fixture
def no_rmtree(monkeypatch):
    """Make any `shutil.rmtree` in setup_demo fail loudly instead of deleting."""

    def _explode(path, *args, **kwargs):
        raise _RmtreeCalled(f"setup_demo tried to delete {path}")

    monkeypatch.setattr(demo_module.shutil, "rmtree", _explode)


def test_setup_demo_creates_a_committed_git_repo(tmp_path):
    env = setup_demo(tmp_path / "sandbox")

    assert env.issue_file.is_file()
    assert env.copilot_cmd.is_file()
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=env.repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "demo sandbox skeleton" in log.stdout


def test_setup_demo_refuses_a_non_empty_directory_unless_reset(tmp_path):
    dest = tmp_path / "sandbox"
    setup_demo(dest)
    with pytest.raises(DemoError):
        setup_demo(dest)
    assert setup_demo(dest, force=True).repo_dir == dest


def test_demo_test_cmd_falls_back_when_pytest_is_absent(monkeypatch):
    """A `uv tool install` venv has no pytest; the demo must still run green."""
    assert "pytest" in demo_test_cmd()
    monkeypatch.setattr(demo_module.importlib.util, "find_spec", lambda name: None)
    fallback = demo_test_cmd()
    assert "minitest.py" in fallback and "pytest" not in fallback


def test_minitest_reports_red_and_green(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    green = tmp_path / "test_green.py"
    green.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    red = tmp_path / "test_red.py"
    red.write_text("def test_bad():\n    assert 1 + 1 == 3\n")

    assert minitest.main([str(green)]) == 0
    assert minitest.main([str(red)]) == 1
    assert minitest.main([str(tmp_path / "missing.py")]) == 2


def test_responder_drives_the_ticket_to_green_only_after_a_retry(tmp_path):
    """The scripted coder is wrong first: that is what shows the harness working."""
    prompt = "You are builder.coder\nSub-task: add median to demo_pkg.stats\n"

    first, _, _ = respond(prompt, tmp_path)
    stats = (tmp_path / "demo_pkg" / "stats.py").read_text()
    assert "sorted" not in stats  # the deliberate first-attempt bug

    respond(prompt, tmp_path)
    respond(prompt, tmp_path)
    assert "sorted" in (tmp_path / "demo_pkg" / "stats.py").read_text()
    assert json.loads(first)["changed_files"] == ["demo_pkg/stats.py"]


def test_responder_streams_copilot_json_events(tmp_path):
    env = setup_demo(tmp_path / "sandbox")
    result = subprocess.run(
        [
            str(env.copilot_cmd),
            "-p",
            "You are the planner in an automated TDD pipeline",
            "--output-format",
            "json",
            "-C",
            str(env.repo_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "ISSUE_RUNNER_DEMO_DELAY": "0"},
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert "assistant.message" in kinds
    assert kinds[-1] == "model.model_call_success"
    message = next(e for e in events if e["type"] == "assistant.message")
    assert len(json.loads(message["data"]["content"])["tickets"]) == 2


def test_demo_runs_the_whole_pipeline_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ISSUE_RUNNER_DEMO_DELAY", "0")
    rc = run_offline_fixture(tmp_path / "sandbox")
    out = capsys.readouterr().out

    assert rc == 0
    assert "tickets done: 2, blocked: 0" in out
    repo = tmp_path / "sandbox"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "ticket-1" in log and "ticket-2" in log
    # the median implementation the scripted loop converged on must be correct
    proof = subprocess.run(
        [sys.executable, "-c", "from demo_pkg.stats import median; print(median([1, 2, 3, 4]))"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proof.stdout.strip() == "2.5"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert dirty.strip() == ""


def _forge_marker(directory: Path) -> None:
    """Write a marker the runner would accept, to prove path guards run first."""
    marker = directory / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"marker": demo_module.MARKER_MAGIC}))


def test_reset_refuses_unowned_directory(tmp_path, no_rmtree):
    """A directory the runner did not create must survive --demo-reset untouched."""
    dest = tmp_path / "real-work"
    (dest / "src").mkdir(parents=True)
    (dest / "src" / "important.py").write_text("value = 42\n")

    with pytest.raises(DemoError) as excinfo:
        setup_demo(dest, force=True)

    assert demo_module.MARKER_NAME in str(excinfo.value)
    assert (dest / "src" / "important.py").read_text() == "value = 42\n"


def test_reset_refuses_unowned_directory_with_a_corrupt_marker(tmp_path, no_rmtree):
    dest = tmp_path / "half-owned"
    (dest / demo_module.STATE_DIR_NAME).mkdir(parents=True)
    (dest / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME).write_text("not json")
    (dest / "keep.txt").write_text("keep me\n")

    with pytest.raises(DemoError):
        setup_demo(dest, force=True)
    assert (dest / "keep.txt").is_file()


def test_reset_refuses_dangerous_paths(tmp_path, monkeypatch, no_rmtree):
    """Root, home, cwd, the project checkout and their ancestors are never sandboxes.

    Each candidate carries a forged ownership marker, so this proves the path
    guards run before ownership is consulted and before any deletion.
    """
    fake_home = tmp_path / "home" / "someone"
    fake_home.mkdir(parents=True)
    _forge_marker(fake_home)
    monkeypatch.setattr(demo_module.Path, "home", classmethod(lambda cls: fake_home))

    work = tmp_path / "work"
    (work / "nested").mkdir(parents=True)
    _forge_marker(work)
    monkeypatch.chdir(work / "nested")

    candidates = [
        Path(tmp_path.anchor),  # filesystem root
        fake_home,
        fake_home.parent,  # ancestor of home
        work / "nested",  # cwd
        work,  # ancestor of cwd
        Path(demo_module.__file__).resolve().parents[3],  # project root
        Path("/tmp"),
        Path("/usr"),
        Path("/etc"),
    ]
    for candidate in candidates:
        with pytest.raises(DemoError, match="refusing to use it as the demo sandbox"):
            setup_demo(candidate, force=True)

    assert (fake_home / demo_module.STATE_DIR_NAME).is_dir()
    assert (work / "nested").is_dir()


def test_setup_demo_refuses_a_symlinked_destination(tmp_path, no_rmtree):
    real = tmp_path / "real"
    real.mkdir()
    (real / "data.txt").write_text("mine\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(DemoError, match="symlink"):
        setup_demo(link, force=True)
    assert (real / "data.txt").is_file()


def test_setup_demo_refuses_a_symlinked_ownership_marker(tmp_path, no_rmtree):
    dest = tmp_path / "sandbox"
    (dest / demo_module.STATE_DIR_NAME).mkdir(parents=True)
    genuine = tmp_path / "genuine.json"
    genuine.write_text(json.dumps({"marker": demo_module.MARKER_MAGIC}))
    (dest / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME).symlink_to(genuine)

    with pytest.raises(DemoError):
        setup_demo(dest, force=True)


def test_reset_of_an_owned_sandbox_still_works(tmp_path):
    dest = tmp_path / "sandbox"
    setup_demo(dest)
    (dest / "scratch.txt").write_text("stale\n")

    env = setup_demo(dest, force=True)

    assert env.repo_dir == dest
    assert not (dest / "scratch.txt").exists()
    assert (dest / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME).is_file()


def test_setup_demo_accepts_an_empty_or_new_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert setup_demo(empty).repo_dir == empty
    assert setup_demo(tmp_path / "brand-new").repo_dir == tmp_path / "brand-new"


def test_marker_is_written_and_kept_out_of_the_index(tmp_path):
    env = setup_demo(tmp_path / "sandbox")
    marker = env.repo_dir / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME

    assert json.loads(marker.read_text())["marker"] == demo_module.MARKER_MAGIC
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=env.repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert dirty.strip() == ""


def test_marker_survives_an_ordinary_demo_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ISSUE_RUNNER_DEMO_DELAY", "0")
    dest = tmp_path / "sandbox"
    assert run_offline_fixture(dest) == 0

    marker = dest / demo_module.STATE_DIR_NAME / demo_module.MARKER_NAME
    assert json.loads(marker.read_text())["marker"] == demo_module.MARKER_MAGIC
    # and the sandbox is still resettable afterwards
    assert setup_demo(dest, force=True).repo_dir == dest


@pytest.mark.parametrize(
    ("attr", "boom", "needle"),
    [
        ("home", RuntimeError("cannot determine home directory"), "home directory"),
        ("cwd", OSError("cwd has been deleted"), "current working directory"),
    ],
)
def test_reset_fails_closed_when_a_protection_path_cannot_be_resolved(
    tmp_path, monkeypatch, no_rmtree, attr, boom, needle
):
    """An unresolvable home/cwd means the guards cannot clear the path, so we stop."""
    dest = tmp_path / "sandbox"
    setup_demo(dest)  # genuinely owned: only the resolution failure may block it
    (dest / "scratch.txt").write_text("stale\n")

    def _explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(demo_module.Path, attr, staticmethod(_explode))

    with pytest.raises(DemoError, match=needle):
        setup_demo(dest, force=True)
    assert (dest / "scratch.txt").is_file()


def test_reset_fails_closed_when_the_destination_cannot_be_resolved(
    tmp_path, monkeypatch, no_rmtree
):
    dest = tmp_path / "sandbox"
    setup_demo(dest)
    (dest / "scratch.txt").write_text("stale\n")

    def _explode(self, *args, **kwargs):
        raise OSError("too many levels of symbolic links")

    monkeypatch.setattr(demo_module.Path, "resolve", _explode)

    with pytest.raises(DemoError, match="demo destination"):
        setup_demo(dest, force=True)
    assert (dest / "scratch.txt").is_file()


def test_reset_fails_closed_when_the_project_root_cannot_be_resolved(
    tmp_path, monkeypatch, no_rmtree
):
    dest = tmp_path / "sandbox"
    setup_demo(dest)
    (dest / "scratch.txt").write_text("stale\n")

    def _explode():
        raise OSError("install tree is gone")

    monkeypatch.setattr(demo_module, "_project_root", _explode)

    with pytest.raises(DemoError, match="project root"):
        setup_demo(dest, force=True)
    assert (dest / "scratch.txt").is_file()
