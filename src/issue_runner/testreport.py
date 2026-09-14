"""Turn a test command's output into *evidence that tests actually ran*.

An exit code alone cannot carry a TDD loop. Exit 0 is produced by a suite that
collected nothing (pytest 8 exits 5, `go test` prints "[no test files]" and
exits 0, jest can be told to pass with no tests), and a non-zero exit is
produced by an import error, a compile failure or a missing binary just as
readily as by a failing assertion. Both mistakes are fatal here: a phantom
green lets a stub through, a phantom red makes the coder chase a bug that does
not exist.

So every run is classified into one of four outcomes by parsing the runner's
own report:

- PASSED   at least one test executed and none failed
- FAILED   at least one test executed and at least one failed
- NO_TESTS the command ran but executed nothing (empty file, all skipped)
- ERROR    the command could not establish a result (collection error, compile
           failure, unrecognised output)

Only PASSED and FAILED are answers; NO_TESTS and ERROR fail closed, and the
caller turns them into corrective feedback. Parsers are deliberately small
readers of stable, user-visible framework summaries — no new dependencies, no
plugin framework, and never a source-code regex pretending to be an oracle.
"""

import re
import shlex
from dataclasses import dataclass
from enum import Enum

__all__ = ["Status", "TestResult", "command_family", "interpret"]


class Status(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NO_TESTS = "no_tests"
    ERROR = "error"


@dataclass(frozen=True)
class TestResult:
    status: Status
    executed: int = 0
    failed: int = 0
    framework: str = "unknown"
    detail: str = ""

    @property
    def is_answer(self) -> bool:
        """True when the run really executed tests, whatever the verdict."""
        return self.status in (Status.PASSED, Status.FAILED)

    @property
    def passed(self) -> bool:
        return self.status is Status.PASSED


def _verdict(framework: str, executed: int, failed: int, detail: str = "") -> TestResult:
    if executed <= 0:
        return TestResult(
            Status.NO_TESTS,
            0,
            0,
            framework,
            detail or "the command ran but executed no test",
        )
    if failed > 0:
        return TestResult(Status.FAILED, executed, failed, framework, detail)
    return TestResult(Status.PASSED, executed, 0, framework, detail)


# --------------------------------------------------------------------------
# TAP — the documented protocol for custom test commands, and what
# `node --test` emits natively.
# --------------------------------------------------------------------------

_TAP_PLAN = re.compile(r"^\s*1\.\.(\d+)", re.MULTILINE)
_TAP_OK = re.compile(r"^\s*ok\s+\d+", re.MULTILINE)
_TAP_NOT_OK = re.compile(r"^\s*not ok\s+\d+", re.MULTILINE)
# `#` is TAP proper, `ℹ` is node's default "spec" reporter, same counters
_TAP_COUNT = re.compile(r"^\s*[#ℹ]\s*(tests|pass|fail|skipped|todo)\s+(\d+)", re.MULTILINE)
_TAP_BAILOUT = re.compile(r"^\s*Bail out!(.*)$", re.MULTILINE)
_TAP_POINT = re.compile(r"^\s*(?:not ok|ok)\s+\d+\s*-?\s*(.*?)\s*$", re.MULTILINE)
_SPEC_POINT = re.compile(r"^\s*[✔✖]\s+(.*?)(?:\s+\([\d.]+ms\))?\s*$", re.MULTILINE)


def _only_the_file_itself(output: str, test_path: str | None) -> bool:
    """`node --test` reports a file with no test() calls as one passing test.

    Exit 0 plus "# pass 1" would otherwise certify an empty file as green, so
    when every reported point is named after the file under test — and nothing
    is nested inside it — there is no real test case. Restricted to node's own
    reporters, so a runner that legitimately reports one point per file is not
    second-guessed.
    """
    if not test_path:
        return False
    if "# Subtest:" not in output and not _SPEC_POINT.search(output):
        return False
    names = [n for n in _TAP_POINT.findall(output) if n] or [
        n for n in _SPEC_POINT.findall(output) if n
    ]
    if not names:
        return False
    aliases = {test_path, test_path.rsplit("/", 1)[-1]}
    return all(name in aliases for name in names)


def _parse_tap(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    bail = _TAP_BAILOUT.search(output)
    if bail:
        return TestResult(
            Status.ERROR, 0, 0, "tap", f"the runner bailed out:{bail.group(1).rstrip()}"
        )
    plan = _TAP_PLAN.search(output)
    counts = {name: int(value) for name, value in _TAP_COUNT.findall(output)}
    ok = len(_TAP_OK.findall(output))
    not_ok = len(_TAP_NOT_OK.findall(output))
    if plan is None and not counts and not (ok or not_ok):
        return None

    if _only_the_file_itself(output, test_path):
        return TestResult(
            Status.NO_TESTS,
            0,
            0,
            "tap",
            f"the runner reported only {test_path} itself, so the file defines no test case",
        )

    # `# pass`/`# fail` are node --test's summary and outrank counted lines,
    # which include nested subtest points.
    if "pass" in counts or "fail" in counts:
        passed, failed = counts.get("pass", 0), counts.get("fail", 0)
        return _verdict("tap", passed + failed, failed)

    if plan is not None and int(plan.group(1)) == 0 and not (ok or not_ok):
        return TestResult(Status.NO_TESTS, 0, 0, "tap", "the TAP plan declared 0 tests")
    if ok or not_ok:
        return _verdict("tap", ok + not_ok, not_ok)
    return TestResult(
        Status.ERROR,
        0,
        0,
        "tap",
        f"TAP plan announced {plan.group(1)} tests but reported no results (exit {returncode})",
    )


# --------------------------------------------------------------------------
# pytest
# --------------------------------------------------------------------------

_PYTEST_MARKERS = (
    "test session starts",
    "short test summary info",
    "no tests ran",
    "ERROR collecting",
    "INTERNALERROR",
    "rootdir:",
    "collected ",
)
# the final summary line, e.g. "1 failed, 2 passed, 1 skipped in 0.12s"
_PYTEST_SUMMARY_LINE = re.compile(r"^.*\b\d+ \w+.*\bin \d+[\d.,]*s.*$", re.MULTILINE)
_PYTEST_COUNT = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_EXECUTED = ("passed", "failed", "xfailed", "xpassed")


def _parse_pytest(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    # `-q` prints no session header, so the summary line is itself a marker.
    summaries = _PYTEST_SUMMARY_LINE.findall(output)
    counts: dict[str, int] = {}
    if summaries:
        for value, name in _PYTEST_COUNT.findall(summaries[-1]):
            counts[name.rstrip("s") if name.startswith("error") else name] = int(value)
    if not counts and not any(marker in output for marker in _PYTEST_MARKERS):
        return None
    if "no tests ran" in output:
        return TestResult(Status.NO_TESTS, 0, 0, "pytest", "pytest collected no test to run")

    if counts.get("error"):
        return TestResult(
            Status.ERROR,
            0,
            0,
            "pytest",
            f"pytest reported {counts['error']} collection/setup error(s) — the test never ran",
        )
    if "ERROR collecting" in output or "INTERNALERROR" in output:
        return TestResult(Status.ERROR, 0, 0, "pytest", "pytest failed to collect the test file")
    if not counts:
        return TestResult(
            Status.ERROR,
            0,
            0,
            "pytest",
            f"pytest exited {returncode} without a result summary",
        )

    executed = sum(counts.get(name, 0) for name in _PYTEST_EXECUTED)
    failed = counts.get("failed", 0)
    if executed == 0 and counts.get("skipped"):
        return TestResult(
            Status.NO_TESTS,
            0,
            0,
            "pytest",
            f"every test was skipped ({counts['skipped']} skipped)",
        )
    return _verdict("pytest", executed, failed)


# --------------------------------------------------------------------------
# jest
# --------------------------------------------------------------------------

_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$", re.MULTILINE)
_JEST_COUNT = re.compile(r"(\d+) (passed|failed|skipped|todo|total)")


def _parse_jest(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    line = _JEST_TESTS.search(output)
    if line is None:
        if "No tests found" in output:
            return TestResult(Status.NO_TESTS, 0, 0, "jest", "jest found no test to run")
        if "Test suite failed to run" in output:
            return TestResult(
                Status.ERROR, 0, 0, "jest", "a jest test suite failed to run (not a test failure)"
            )
        return None
    counts = {name: int(value) for value, name in _JEST_COUNT.findall(line.group(1))}
    if "Test suite failed to run" in output and not counts.get("failed"):
        return TestResult(
            Status.ERROR, 0, 0, "jest", "a jest test suite failed to run (not a test failure)"
        )
    executed = counts.get("passed", 0) + counts.get("failed", 0)
    return _verdict("jest", executed, counts.get("failed", 0))


# --------------------------------------------------------------------------
# vitest
# --------------------------------------------------------------------------

_VITEST_TESTS = re.compile(r"^\s*Tests\s+(.*)$", re.MULTILINE)
_VITEST_COUNT = re.compile(r"(\d+) (passed|failed|skipped|todo)")


_VITEST_NO_FILES = re.compile(r"No test (files )?found")


def _parse_vitest(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    if _VITEST_NO_FILES.search(output):
        return TestResult(Status.NO_TESTS, 0, 0, "vitest", "vitest found no test file")
    if "Test Files" not in output and "vitest" not in output.lower():
        return None
    line = _VITEST_TESTS.search(output)
    if line is None:
        if "Test Files" in output:
            return TestResult(
                Status.ERROR,
                0,
                0,
                "vitest",
                f"vitest exited {returncode} without a test summary (likely a "
                "transform or import error)",
            )
        return None
    counts = {name: int(value) for value, name in _VITEST_COUNT.findall(line.group(1))}
    executed = counts.get("passed", 0) + counts.get("failed", 0)
    return _verdict("vitest", executed, counts.get("failed", 0))


# --------------------------------------------------------------------------
# go test
# --------------------------------------------------------------------------

_GO_PASS = re.compile(r"^\s*--- PASS: ", re.MULTILINE)
_GO_FAIL = re.compile(r"^\s*--- FAIL: ", re.MULTILINE)
_GO_SKIP = re.compile(r"^\s*--- SKIP: ", re.MULTILINE)
_GO_NO_FILES = re.compile(r"^\?\s+\S+\s+\[no test files\]", re.MULTILINE)
_GO_BUILD_FAIL = re.compile(r"\[(build failed|setup failed)\]|^# \S+", re.MULTILINE)


def _parse_go(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    markers = ("--- PASS", "--- FAIL", "--- SKIP", "no test files", "no tests to run")
    is_go = any(m in output for m in markers) or re.search(
        r"^(ok|FAIL)\s+\S+", output, re.MULTILINE
    )
    if not is_go:
        return None
    if _GO_BUILD_FAIL.search(output):
        return TestResult(Status.ERROR, 0, 0, "go", "the go package failed to build — no test ran")
    passed = len(_GO_PASS.findall(output))
    failed = len(_GO_FAIL.findall(output))
    if passed + failed == 0:
        why = "the package contains no test files"
        if _GO_SKIP.search(output):
            why = "every go test was skipped"
        elif not _GO_NO_FILES.search(output) and "no tests to run" not in output:
            why = f"go test exited {returncode} without reporting a single test"
        return TestResult(Status.NO_TESTS, 0, 0, "go", why)
    return _verdict("go", passed + failed, failed)


# --------------------------------------------------------------------------
# cargo test
# --------------------------------------------------------------------------

_CARGO_RESULT = re.compile(
    r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed;(?: (\d+) ignored;)?"
)


def _parse_cargo(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    results = _CARGO_RESULT.findall(output)
    compile_failed = "error: could not compile" in output or re.search(
        r"^error\[E\d+\]", output, re.MULTILINE
    )
    if compile_failed:
        return TestResult(Status.ERROR, 0, 0, "cargo", "the crate failed to compile — no test ran")
    if not results:
        return None
    passed = sum(int(r[1]) for r in results)
    failed = sum(int(r[2]) for r in results)
    return _verdict("cargo", passed + failed, failed)


# pytest goes last in the default order: its summary line is the loosest
# signature (cargo's "…0 ignored; finished in 0.00s" would match it), so every
# stricter parser gets first refusal.
_PARSERS = {
    "tap": _parse_tap,
    "jest": _parse_jest,
    "vitest": _parse_vitest,
    "go": _parse_go,
    "cargo": _parse_cargo,
    "pytest": _parse_pytest,
}

# a family hint only reorders the parsers; the output still has the last word,
# because `npm test` is just as likely to shell out to pytest as to jest.
_FAMILY_ORDER: dict[str, tuple[str, ...]] = {
    "pytest": ("pytest", "tap"),
    "js": ("tap", "jest", "vitest"),
    "go": ("go",),
    "cargo": ("cargo",),
}

_JS_COMMANDS = {"npm", "npx", "yarn", "pnpm", "node", "jest", "vitest", "bun", "deno"}


def command_family(command: str) -> str:
    """Best guess at which runner `command` drives — a hint, never a verdict."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return "unknown"
    names = [token.rsplit("/", 1)[-1] for token in tokens]
    if any(name == "pytest" or name.startswith("pytest") for name in names):
        return "pytest"
    if "-m" in tokens:
        index = tokens.index("-m")
        if index + 1 < len(tokens) and tokens[index + 1] in ("pytest", "unittest"):
            return "pytest" if tokens[index + 1] == "pytest" else "unittest"
    head = names[0]
    if head in _JS_COMMANDS:
        return "js"
    if head == "go":
        return "go"
    if head in ("cargo", "cross"):
        return "cargo"
    return "unknown"


_NO_BINARY = re.compile(r"(command not found|No such file or directory|is not recognized)")


def interpret(
    command: str, output: str, returncode: int, test_path: str | None = None
) -> TestResult:
    """Classify one finished test command. Never raises."""
    family = command_family(command)
    order = list(_FAMILY_ORDER.get(family, ()))
    order += [name for name in _PARSERS if name not in order]
    for name in order:
        result = _PARSERS[name](output, returncode, test_path)
        if result is not None:
            return result

    if returncode == 127 or (returncode != 0 and _NO_BINARY.search(output)):
        return TestResult(
            Status.ERROR,
            0,
            0,
            family,
            f"the test command could not be executed (exit {returncode}) — check that "
            "the runner is installed and on PATH",
        )
    return TestResult(
        Status.ERROR,
        0,
        0,
        family,
        f"the test command exited {returncode} but produced no recognisable test "
        "report, so no test is known to have run. Supported reports: pytest, jest, "
        "vitest, go test, cargo test, or TAP (a '1..N' plan with 'ok'/'not ok' lines).",
    )
