"""Execution evidence: a run only counts when a test really executed.

Three layers are covered:
- `testreport.interpret`, against real captured runner output plus bounded
  protocol fixtures for runners not installed here;
- `run_tests` / `run_test_command`, which turn evidence into a verdict or a
  BuildError — never into a guess from an exit code;
- `resolve_test_path`, which keeps untrusted model-supplied paths inside the
  workspace.
"""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from issue_runner.config import RunnerConfig
from issue_runner.phases.build import (
    BuildError,
    resolve_test_path,
    run_test_command,
    run_tests,
)
from issue_runner.testcmd import detect_regression_cmd
from issue_runner.testreport import Status, command_family, interpret

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def py_cfg(repo: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_dir=repo,
        test_cmd=f"{sys.executable} -m pytest {{test_path}} -q",
        github_tickets=False,
    )


def write(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(textwrap.dedent(body))
    return name


# --------------------------------------------------------------------------
# pytest, executed for real
# --------------------------------------------------------------------------


def test_passing_pytest_file_is_green(tmp_path):
    path = write(tmp_path, "test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    passed, output = run_tests(py_cfg(tmp_path), path)
    assert passed is True
    assert "1 passed" in output


def test_failing_pytest_file_is_red_not_an_error(tmp_path):
    path = write(tmp_path, "test_bad.py", "def test_bad():\n    assert 1 + 1 == 3\n")
    passed, output = run_tests(py_cfg(tmp_path), path)
    assert passed is False
    assert "1 failed" in output


def test_comment_only_file_can_be_neither_red_nor_green(tmp_path):
    path = write(tmp_path, "test_empty.py", "# assert something one day\n")
    with pytest.raises(BuildError) as exc:
        run_tests(py_cfg(tmp_path), path)
    assert "no usable test result" in str(exc.value)


def test_skipped_only_file_is_not_a_result(tmp_path):
    path = write(
        tmp_path,
        "test_skip.py",
        """\
        import pytest

        @pytest.mark.skip(reason="not yet")
        def test_later():
            assert False
        """,
    )
    with pytest.raises(BuildError, match="skipped"):
        run_tests(py_cfg(tmp_path), path)


def test_collection_error_is_not_a_failing_test(tmp_path):
    """A module-level import error is a fault to fix, not evidence of red."""
    path = write(
        tmp_path,
        "test_broken.py",
        "from nowhere_at_all import thing\n\ndef test_x():\n    assert thing()\n",
    )
    with pytest.raises(BuildError) as exc:
        run_tests(py_cfg(tmp_path), path)
    assert "error" in str(exc.value).lower()


def test_missing_behaviour_inside_an_executed_test_is_red(tmp_path):
    """The TDD red case: the import lives inside the test, so a case runs."""
    path = write(
        tmp_path,
        "test_red.py",
        """\
        def test_mean():
            from demo_pkg.stats import mean

            assert mean([1, 2, 3]) == 2
        """,
    )
    passed, output = run_tests(py_cfg(tmp_path), path)
    assert passed is False
    assert "1 failed" in output


def test_missing_binary_raises_build_error_not_a_subprocess_error(tmp_path):
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        test_cmd="definitely-not-a-real-runner {test_path}",
        github_tickets=False,
    )
    with pytest.raises(BuildError, match="could not be run"):
        run_tests(cfg, "test_x.py")


def test_unparseable_command_raises_build_error(tmp_path):
    cfg = RunnerConfig(repo_dir=tmp_path, test_cmd="runner 'unclosed", github_tickets=False)
    with pytest.raises(BuildError, match="could not be parsed"):
        run_tests(cfg, "test_x.py")


def test_command_producing_no_report_fails_closed(tmp_path):
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        test_cmd=f"{sys.executable} -c pass",
        github_tickets=False,
    )
    with pytest.raises(BuildError, match="no recognisable test report"):
        run_tests(cfg, "test_x.py")


def test_exit_zero_with_no_report_is_still_not_green(tmp_path):
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        test_cmd=f"{sys.executable} -c 'print(\"all good!\")'",
        github_tickets=False,
    )
    with pytest.raises(BuildError):
        run_tests(cfg, "test_x.py")


# --------------------------------------------------------------------------
# run_test_command (the whole-suite entry point)
# --------------------------------------------------------------------------


def test_run_test_command_runs_a_whole_directory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n")
    cfg = py_cfg(tmp_path)
    passed, output = run_test_command(cfg, f"{sys.executable} -m pytest -q tests")
    assert passed is True
    assert "2 passed" in output


def test_run_test_command_reports_a_real_failure(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert False\n")
    passed, _ = run_test_command(py_cfg(tmp_path), f"{sys.executable} -m pytest -q .")
    assert passed is False


def test_run_test_command_rejects_an_empty_suite(tmp_path):
    with pytest.raises(BuildError):
        run_test_command(py_cfg(tmp_path), f"{sys.executable} -m pytest -q .")


# --------------------------------------------------------------------------
# path containment
# --------------------------------------------------------------------------


def test_resolve_test_path_accepts_a_relative_path_in_the_repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    resolved = resolve_test_path(py_cfg(tmp_path), "tests/test_x.py")
    assert resolved == (tmp_path / "tests" / "test_x.py").resolve()


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside/test_x.py", "tests/../../test_x.py", "", "   "],
)
def test_resolve_test_path_rejects_escapes(tmp_path, bad):
    with pytest.raises(BuildError):
        resolve_test_path(py_cfg(tmp_path), bad)


def test_resolve_test_path_rejects_a_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("# not yours\n")
    (repo / "link").symlink_to(outside)
    with pytest.raises(BuildError, match="outside"):
        resolve_test_path(py_cfg(repo), "link/secret.py")


def test_escaping_test_path_is_never_run(tmp_path):
    with pytest.raises(BuildError):
        resolve_test_path(py_cfg(tmp_path), "../../etc/passwd")


# --------------------------------------------------------------------------
# framework report adapters — real captured output shapes
# --------------------------------------------------------------------------

PYTEST_QUIET_PASS = ".                                       [100%]\n1 passed in 0.01s\n"
PYTEST_QUIET_FAIL = (
    "F                                       [100%]\n"
    "=========================== short test summary info ===========================\n"
    "FAILED test_e.py::test_fail - assert 1 == 2\n"
    "1 failed in 0.01s\n"
)
PYTEST_NO_TESTS = "\nno tests ran in 0.00s\n"
PYTEST_COLLECTION_ERROR = (
    "E   ModuleNotFoundError: No module named 'nope'\n"
    "=========================== short test summary info ===========================\n"
    "ERROR test_c.py\n"
    "!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!\n"
    "1 error in 0.06s\n"
)
PYTEST_MIXED = "1 failed, 2 passed, 1 skipped in 0.12s\n"

JEST_PASS = (
    "PASS  ./sum.test.js\nTest Suites: 1 passed, 1 total\n"
    "Tests:       2 passed, 2 total\nSnapshots:   0 total\nTime:        0.5 s\n"
)
JEST_FAIL = (
    "FAIL  ./sum.test.js\n  ● adds\n    expect(received).toBe(expected)\n"
    "Test Suites: 1 failed, 1 total\nTests:       1 failed, 1 passed, 2 total\n"
)
JEST_NO_TESTS = "No tests found, exiting with code 1\n"
JEST_SUITE_ERROR = (
    "FAIL ./sum.test.js\n  ● Test suite failed to run\n\n    Cannot find module './sum'\n"
    "Test Suites: 1 failed, 1 total\nTests:       0 total\n"
)

VITEST_PASS = (
    " RUN  v1.6.0 /repo\n ✓ sum.test.ts (2)\n\n Test Files  1 passed (1)\n"
    "      Tests  2 passed (2)\n   Duration  0.40s\n"
)
VITEST_FAIL = (
    " ❯ sum.test.ts (2)\n   × adds\n\n Test Files  1 failed (1)\n"
    "      Tests  1 failed | 1 passed (2)\n"
)
VITEST_NO_FILES = " RUN  v1.6.0 /repo\n\n No test files found, exiting with code 1\n"

GO_PASS = "=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\nPASS\nok  \texample.com/x\t0.003s\n"
GO_FAIL = (
    "=== RUN   TestAdd\n    add_test.go:9: got 4 want 3\n--- FAIL: TestAdd (0.00s)\n"
    "FAIL\nexit status 1\nFAIL\texample.com/x\t0.004s\n"
)
GO_NO_TEST_FILES = "?   \texample.com/x\t[no test files]\n"
GO_BUILD_FAILED = (
    "# example.com/x [example.com/x.test]\n./add_test.go:6:9: undefined: Add\n"
    "FAIL\texample.com/x [build failed]\n"
)

CARGO_PASS = (
    "   Compiling x v0.1.0\n     Running unittests src/lib.rs\n\nrunning 2 tests\n"
    "test tests::ok_case ... ok\ntest tests::other ... ok\n\n"
    "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
    "finished in 0.00s\n"
)
CARGO_FAIL = (
    "running 2 tests\ntest tests::bad_case ... FAILED\ntest tests::ok_case ... ok\n\n"
    "failures:\n    tests::bad_case\n\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
    "finished in 0.00s\n\nerror: test failed, to rerun pass `--lib`\n"
)
CARGO_EMPTY = (
    "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; "
    "0 filtered out; finished in 0.00s\n"
)
CARGO_COMPILE_ERROR = (
    "error[E0425]: cannot find function `add` in this scope\n"
    " --> src/lib.rs:5:24\n\nerror: could not compile `x` (lib test) due to 1 error\n"
)

NODE_TAP_FAIL = (
    "TAP version 13\n# Subtest: adds\nok 1 - adds\n  ---\n  duration_ms: 0.4\n  ...\n"
    "# Subtest: bad\nnot ok 2 - bad\n  ---\n  failureType: 'testCodeFailure'\n  ...\n"
    "1..2\n# tests 2\n# suites 0\n# pass 1\n# fail 1\n# cancelled 0\n# skipped 0\n"
)
NODE_SPEC_PASS = "✔ adds (0.5ms)\nℹ tests 1\nℹ suites 0\nℹ pass 1\nℹ fail 0\nℹ skipped 0\n"


@pytest.mark.parametrize(
    "command,output,returncode,status,executed",
    [
        ("pytest {p} -q", PYTEST_QUIET_PASS, 0, Status.PASSED, 1),
        ("pytest {p} -q", PYTEST_QUIET_FAIL, 1, Status.FAILED, 1),
        ("pytest {p} -q", PYTEST_NO_TESTS, 5, Status.NO_TESTS, 0),
        ("pytest {p} -q", PYTEST_COLLECTION_ERROR, 2, Status.ERROR, 0),
        ("pytest {p} -q", PYTEST_MIXED, 1, Status.FAILED, 3),
        ("npm test", JEST_PASS, 0, Status.PASSED, 2),
        ("npm test", JEST_FAIL, 1, Status.FAILED, 2),
        ("npm test", JEST_NO_TESTS, 1, Status.NO_TESTS, 0),
        ("npm test", JEST_SUITE_ERROR, 1, Status.ERROR, 0),
        ("npx vitest run", VITEST_PASS, 0, Status.PASSED, 2),
        ("npx vitest run", VITEST_FAIL, 1, Status.FAILED, 2),
        ("npx vitest run", VITEST_NO_FILES, 1, Status.NO_TESTS, 0),
        ("go test ./...", GO_PASS, 0, Status.PASSED, 1),
        ("go test ./...", GO_FAIL, 1, Status.FAILED, 1),
        ("go test ./...", GO_NO_TEST_FILES, 0, Status.NO_TESTS, 0),
        ("go test ./...", GO_BUILD_FAILED, 2, Status.ERROR, 0),
        ("cargo test", CARGO_PASS, 0, Status.PASSED, 2),
        ("cargo test", CARGO_FAIL, 101, Status.FAILED, 2),
        ("cargo test", CARGO_EMPTY, 0, Status.NO_TESTS, 0),
        ("cargo test", CARGO_COMPILE_ERROR, 101, Status.ERROR, 0),
        ("node --test", NODE_TAP_FAIL, 1, Status.FAILED, 2),
        ("node --test", NODE_SPEC_PASS, 0, Status.PASSED, 1),
    ],
)
def test_interpret_classifies_framework_reports(command, output, returncode, status, executed):
    report = interpret(command, output, returncode)
    assert report.status is status
    assert report.executed == executed


def test_normal_js_assertions_are_not_rejected_for_their_syntax(tmp_path):
    """`expect(x).toBe(y)` carries no Python `assert`; the report decides."""
    report = interpret("npm test -- sum.test.js", JEST_FAIL, 1)
    assert report.status is Status.FAILED


def test_go_and_rust_assertions_are_not_rejected_for_their_syntax():
    assert interpret("go test ./...", GO_FAIL, 1).status is Status.FAILED
    assert interpret("cargo test", CARGO_FAIL, 101).status is Status.FAILED


def test_tap_bail_out_is_an_error():
    report = interpret("./run-tests", "TAP version 13\nBail out! database is down\n", 1)
    assert report.status is Status.ERROR
    assert "bailed out" in report.detail


def test_tap_zero_plan_is_no_tests():
    report = interpret("./run-tests", "TAP version 13\n1..0 # nothing to do\n", 0)
    assert report.status is Status.NO_TESTS


def test_tap_points_are_counted():
    output = "TAP version 13\n1..3\nok 1 - a\nnot ok 2 - b\nok 3 - c\n"
    report = interpret("./run-tests", output, 1)
    assert (report.status, report.executed, report.failed) == (Status.FAILED, 3, 1)


def test_node_reporting_only_the_file_itself_is_not_green():
    """node --test counts a file with no test() call as one passing test."""
    output = (
        "TAP version 13\n# Subtest: empty.test.js\nok 1 - empty.test.js\n  ---\n  ...\n"
        "1..1\n# tests 1\n# pass 1\n# fail 0\n"
    )
    # knowing which file was under test is what makes the wrapper detectable;
    # run_tests always supplies it
    assert interpret("node --test empty.test.js", output, 0, "empty.test.js").status is (
        Status.NO_TESTS
    )
    assert interpret("node --test x.test.js", output, 0, "x.test.js").status is Status.PASSED


def test_unrecognised_output_is_an_error_not_a_guess():
    assert interpret("./mytests", "everything is fine\n", 0).status is Status.ERROR
    assert interpret("./mytests", "something broke\n", 1).status is Status.ERROR


def test_missing_binary_output_is_an_error():
    report = interpret("jest", "sh: 1: jest: command not found\n", 127)
    assert report.status is Status.ERROR
    assert "PATH" in report.detail


@pytest.mark.parametrize(
    "command,family",
    [
        ("/usr/bin/python3 -m pytest x -q", "pytest"),
        ("pytest -q", "pytest"),
        ("npm test -- x", "js"),
        ("npx vitest run", "js"),
        ("node --test", "js"),
        ("go test ./...", "go"),
        ("cargo test", "cargo"),
        ("./my-custom-runner", "unknown"),
        ("", "unknown"),
    ],
)
def test_command_family(command, family):
    assert command_family(command) == family


# --------------------------------------------------------------------------
# real runners, when this machine has them
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_test_runner_for_real(tmp_path):
    (tmp_path / "sum.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('adds', () => { assert.strictEqual(1 + 1, 2); });\n"
        "test('bad', () => { assert.strictEqual(1 + 1, 3); });\n"
    )
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        test_cmd="node --test --test-reporter=tap {test_path}",
        github_tickets=False,
    )
    passed, output = run_tests(cfg, "sum.test.js")
    assert passed is False
    assert "not ok" in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_empty_test_file_is_not_green(tmp_path):
    (tmp_path / "empty.test.js").write_text("// nothing here yet\n")
    cfg = RunnerConfig(
        repo_dir=tmp_path,
        test_cmd="node --test --test-reporter=tap {test_path}",
        github_tickets=False,
    )
    with pytest.raises(BuildError):
        run_tests(cfg, "empty.test.js")


def _cargo_works(tmp_path: Path) -> bool:
    if shutil.which("cargo") is None:
        return False
    probe = subprocess.run(["cargo", "--version"], capture_output=True, text=True, check=False)
    return probe.returncode == 0


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_cargo_test_for_real(tmp_path):
    if not _cargo_works(tmp_path):
        pytest.skip("cargo is not usable here")
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (tmp_path / "src" / "lib.rs").write_text(
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n"
        "    #[test] fn ok_case() { assert_eq!(add(1, 2), 3); }\n"
        "    #[test] fn bad_case() { assert_eq!(add(1, 2), 4); }\n}\n"
    )
    cfg = RunnerConfig(repo_dir=tmp_path, test_cmd="cargo test --offline", github_tickets=False)
    try:
        passed, output = run_tests(cfg, "src/lib.rs")
    except BuildError as e:  # a sandbox without a usable toolchain
        pytest.skip(f"cargo could not run here: {e}")
    assert passed is False
    assert "1 failed" in output


# --------------------------------------------------------------------------
# the bundled dependency-free runner speaks the same protocol
# --------------------------------------------------------------------------


def minitest_cfg(repo: Path) -> RunnerConfig:
    from issue_runner.demo import minitest

    return RunnerConfig(
        repo_dir=repo,
        test_cmd=f"{sys.executable} {Path(minitest.__file__)} {{test_path}}",
        github_tickets=False,
    )


def test_minitest_red_and_green_are_real_results(tmp_path):
    (tmp_path / "test_red.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n")
    (tmp_path / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    cfg = minitest_cfg(tmp_path)
    assert run_tests(cfg, "test_red.py")[0] is False
    assert run_tests(cfg, "test_green.py")[0] is True


def test_minitest_comment_only_file_is_no_result(tmp_path):
    (tmp_path / "test_empty.py").write_text("# assert one day\n")
    with pytest.raises(BuildError):
        run_tests(minitest_cfg(tmp_path), "test_empty.py")


def test_minitest_collection_error_is_no_result(tmp_path):
    (tmp_path / "test_broken.py").write_text("import nowhere_at_all\n")
    with pytest.raises(BuildError):
        run_tests(minitest_cfg(tmp_path), "test_broken.py")


def test_minitest_runs_a_whole_directory(tmp_path):
    from issue_runner.demo import minitest

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tests / "test_b.py").write_text("def test_b():\n    assert True\n")
    cfg = minitest_cfg(tmp_path)
    passed, output = run_test_command(cfg, f"{sys.executable} {Path(minitest.__file__)} tests")
    assert passed is True
    assert "# pass 2" in output


# --------------------------------------------------------------------------
# regression command detection
# --------------------------------------------------------------------------


def test_regression_cmd_has_no_test_path_placeholder(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cmd = detect_regression_cmd(tmp_path)
    assert cmd and "{test_path}" not in cmd
    assert "pytest" in cmd


@pytest.mark.parametrize(
    "marker,content,expected",
    [
        ("go.mod", "module x\n", "go test ./..."),
        ("Cargo.toml", "[package]\n", "cargo test"),
        ("package.json", '{"scripts": {"test": "vitest run"}}', "npm test"),
    ],
)
def test_regression_cmd_per_ecosystem(tmp_path, marker, content, expected):
    (tmp_path / marker).write_text(content)
    assert detect_regression_cmd(tmp_path) == expected


def test_regression_cmd_is_none_for_an_unrecognised_repo(tmp_path):
    assert detect_regression_cmd(tmp_path) is None


def test_regression_cmd_ignores_package_json_without_a_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}')
    assert detect_regression_cmd(tmp_path) is None
