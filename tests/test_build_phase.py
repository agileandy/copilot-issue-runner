import json

import pytest

from issue_runner.phases.build import BuildError, coder_step, run_tests, tester_step
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


def test_tester_accepts_failing_real_test(repo, cfg):
    client = FakeClient([(reply(), write_test_file(repo, "assert RED  # real failing test"))])
    path = tester_step(client, cfg, ticket())
    assert path == "test_subtract.py"


def test_tester_rejects_test_with_no_assertion(repo, cfg):
    client = FakeClient(
        [
            (reply(), write_test_file(repo, "x = 1  # stub, no assertion")),
            (reply(), write_test_file(repo, "assert RED  # fixed")),
        ]
    )
    path = tester_step(client, cfg, ticket())
    assert path == "test_subtract.py"
    assert len(client.calls) == 2
    # the retry prompt must tell the tester what was wrong
    assert "assertion" in client.calls[1]["prompt"].lower()


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
