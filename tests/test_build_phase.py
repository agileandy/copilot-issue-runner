import json
import sys

import pytest

from issue_runner.phases.build import (
    BuildError,
    coder_step,
    resolve_test_path,
    run_tests,
    tester_step,
)
from issue_runner.tickets import Ticket
from tests.conftest import FakeClient


def ticket():
    return Ticket(
        id=1,
        title="add subtract",
        description="add a subtract function",
        test_assertion="subtract(5, 3) == 2",
    )


def write_test_file(repo, content):
    def _side_effect():
        (repo / "test_subtract.py").write_text(content)

    return _side_effect


def reply(path="test_subtract.py"):
    return json.dumps({"test_path": path})


def test_run_tests_green_and_red(repo, cfg):
    (repo / "t.py").write_text("assert PASS")
    passed, _ = run_tests(cfg, "t.py")
    assert passed is True
    (repo / "t.py").write_text("assert RED")
    passed, output = run_tests(cfg, "t.py")
    assert passed is False
    assert "checker ran" in output


def test_run_tests_rejects_a_run_with_no_executed_test(repo, cfg):
    (repo / "t.py").write_text("# no marker, so the checker reports 1..0\n")
    with pytest.raises(BuildError, match="no usable test result"):
        run_tests(cfg, "t.py")


def test_run_test_command_takes_a_complete_command(repo, cfg):
    from issue_runner.phases.build import run_test_command

    (repo / "t.py").write_text("assert PASS")
    passed, output = run_test_command(cfg, f"{sys.executable} {repo / 'checker.py'} t.py")
    assert passed is True
    assert "ok 1" in output


def test_the_fixture_checker_runs_the_whole_tree_for_a_regression_gate(repo, cfg):
    """What the parent's regression gate will drive through run_test_command."""
    from issue_runner.phases.build import run_test_command

    whole_suite = f"{sys.executable} {repo / 'checker.py'} ."
    (repo / "test_a.py").write_text("assert PASS")
    (repo / "test_b.py").write_text("assert PASS")
    (repo / "notes.py").write_text("# no marker, not a test\n")
    passed, output = run_test_command(cfg, whole_suite)
    assert passed is True
    assert "# pass 2" in output and "# fail 0" in output

    (repo / "test_c.py").write_text("assert RED")
    passed, output = run_test_command(cfg, whole_suite)
    assert passed is False
    assert "# fail 1" in output


def test_the_fixture_checker_reports_an_empty_tree_as_no_tests(repo, cfg):
    from issue_runner.phases.build import run_test_command

    with pytest.raises(BuildError, match="no usable test result"):
        run_test_command(cfg, f"{sys.executable} {repo / 'checker.py'} .")


def test_run_tests_rejects_a_path_outside_the_repo(repo, cfg):
    with pytest.raises(BuildError):
        resolve_test_path(cfg, "../escape.py")


def test_tester_accepts_failing_real_test(repo, cfg):
    client = FakeClient([(reply(), write_test_file(repo, "assert RED  # real failing test"))])
    path = tester_step(client, cfg, ticket())
    assert path == "test_subtract.py"


def test_tester_rejects_a_test_that_never_executes(repo, cfg):
    client = FakeClient(
        [
            (reply(), write_test_file(repo, "x = 1  # no test case here")),
            (reply(), write_test_file(repo, "assert RED  # fixed")),
        ]
    )
    path = tester_step(client, cfg, ticket())
    assert path == "test_subtract.py"
    assert len(client.calls) == 2
    # the retry prompt must tell the tester what was wrong
    assert "did not actually run" in client.calls[1]["prompt"]


def test_tester_rejects_a_comment_only_test_without_running_it(repo, cfg):
    client = FakeClient(
        [
            (reply(), write_test_file(repo, "# TODO: assert subtract(5, 3) == 2\n")),
            (reply(), write_test_file(repo, "assert RED  # fixed")),
        ]
    )
    assert tester_step(client, cfg, ticket()) == "test_subtract.py"
    assert "only comments" in client.calls[1]["prompt"]


def test_tester_rejects_a_test_path_outside_the_repo(repo, cfg):
    client = FakeClient(
        [
            (reply("../escape.py"), None),
            (reply(), write_test_file(repo, "assert RED")),
        ]
    )
    assert tester_step(client, cfg, ticket()) == "test_subtract.py"
    assert "traverse outside" in client.calls[1]["prompt"]


def test_tester_rejects_trivially_green_test(repo, cfg):
    client = FakeClient(
        [
            (reply(), write_test_file(repo, "assert PASS  # passes with no code!")),
            (reply(), write_test_file(repo, "assert RED")),
        ]
    )
    tester_step(client, cfg, ticket())
    assert "fail" in client.calls[1]["prompt"].lower()


def test_tester_gives_up_after_retries(repo, cfg):
    client = FakeClient(
        [
            (reply(), write_test_file(repo, "no assertion 1")),
            (reply(), write_test_file(repo, "no assertion 2")),
        ]
    )
    with pytest.raises(BuildError):
        tester_step(client, cfg, ticket())


def test_tester_refine_mode_allows_green_result(repo, cfg):
    # when refining with code already present, the refined test may already pass
    client = FakeClient([(reply(), write_test_file(repo, "assert PASS  # code exists"))])
    path = tester_step(client, cfg, ticket(), feedback="cover negative numbers", require_red=False)
    assert path == "test_subtract.py"


def test_coder_makes_test_green(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")
    client = FakeClient([("done", lambda: (repo / "impl.py").write_text("code"))])
    coder_step(client, cfg, ticket(), "test_subtract.py")
    assert (repo / "impl.py").exists()


def test_coder_rejected_if_it_tampers_with_test(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")

    def cheat():
        (repo / "test_subtract.py").write_text("assert PASS  # weakened the test")

    client = FakeClient([("done", cheat), ("done", cheat)])
    with pytest.raises(BuildError, match="test file"):
        coder_step(client, cfg, ticket(), "test_subtract.py")


def test_coder_retries_then_fails(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")
    client = FakeClient([("done", None), ("done again", None)])
    with pytest.raises(BuildError):
        coder_step(client, cfg, ticket(), "test_subtract.py")


def test_tester_raises_already_passes_when_final_attempt_is_green(repo, cfg):
    from issue_runner.phases.build import TestAlreadyPasses

    client = FakeClient(
        [
            (reply(), write_test_file(repo, "assert PASS  # attempt 1: green")),
            (reply(), write_test_file(repo, "assert PASS  # attempt 2: still green")),
        ]
    )
    with pytest.raises(TestAlreadyPasses) as exc:
        tester_step(client, cfg, ticket())
    assert exc.value.test_path == "test_subtract.py"


# --- test-file restoration (production failure: "could not be restored from git") ---
#
# The test file is written by the tester during THIS run and is not committed
# until the verifier passes, so `git checkout -- <path>` could never restore it:
# for a new file the pathspec does not match, and for a tracked file it reverts
# to HEAD, destroying the tester's work. Restoration uses an in-memory snapshot.


import subprocess


def git_repo_with_committed_test(repo, content):
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "test_subtract.py").write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    return repo


def test_tampered_new_test_file_is_restored_and_the_coder_continues(repo, cfg):
    """The test file is untracked, so git could never have restored it."""
    (repo / "test_subtract.py").write_text("assert RED")

    def cheat():
        (repo / "test_subtract.py").write_text("assert PASS  # weakened")

    def honest():
        (repo / "impl.py").write_text("code")

    client = FakeClient([("done", cheat), ("done", honest)])
    coder_step(client, cfg, ticket(), "test_subtract.py")
    assert (repo / "test_subtract.py").read_text() == "assert RED", "the spec must survive"
    assert (repo / "impl.py").exists()


def test_tampered_tracked_test_file_is_restored_to_the_testers_version(repo, cfg):
    """Regression: git checkout would revert to HEAD and delete the new test."""
    git_repo_with_committed_test(repo, "assert OLD COMMITTED\n")
    tester_version = "assert OLD COMMITTED\nassert RED  # added by the tester this run\n"
    (repo / "test_subtract.py").write_text(tester_version)

    def cheat():
        (repo / "test_subtract.py").write_text("assert PASS\n")

    def honest():
        (repo / "impl.py").write_text("code")

    client = FakeClient([("done", cheat), ("done", honest)])
    coder_step(client, cfg, ticket(), "test_subtract.py")
    assert (repo / "test_subtract.py").read_text() == tester_version


def test_deleted_test_file_is_restored(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")

    def delete_it():
        (repo / "test_subtract.py").unlink()

    def honest():
        (repo / "impl.py").write_text("code")

    client = FakeClient([("done", delete_it), ("done", honest)])
    coder_step(client, cfg, ticket(), "test_subtract.py")
    assert (repo / "test_subtract.py").read_text() == "assert RED"


def test_repeated_tampering_still_fails_the_ticket_cleanly(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")

    def cheat():
        (repo / "test_subtract.py").write_text("assert PASS")

    client = FakeClient([("done", cheat), ("done", cheat)])
    with pytest.raises(BuildError, match="modified the test file"):
        coder_step(client, cfg, ticket(), "test_subtract.py")
    assert (repo / "test_subtract.py").read_text() == "assert RED"


def test_the_coder_is_told_it_tampered(repo, cfg):
    (repo / "test_subtract.py").write_text("assert RED")

    def cheat():
        (repo / "test_subtract.py").write_text("assert PASS")

    def honest():
        (repo / "impl.py").write_text("code")

    client = FakeClient([("done", cheat), ("done", honest)])
    coder_step(client, cfg, ticket(), "test_subtract.py")
    assert "modified the test file" in client.calls[1]["prompt"]
