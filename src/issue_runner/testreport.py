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

__all__ = ["Status", "TestResult", "command_family", "interpret", "strip_ansi"]


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

_TAP_PLAN = re.compile(r"^(?P<indent>[ ]*)1\.\.(?P<count>\d+)")
_TAP_POINT_LINE = re.compile(
    r"^(?P<indent>[ ]*)(?P<ok>not ok|ok)\s+\d+\s*(?:-\s*)?(?P<name>.*?)\s*$"
)
# `#` is TAP proper, `ℹ` is node's default "spec" reporter, same counters
_TAP_COUNT = re.compile(r"^\s*[#ℹ]\s*(tests|pass|fail|skipped|todo)\s+(\d+)", re.MULTILINE)
_TAP_BAILOUT = re.compile(r"^\s*Bail out!(.*)$", re.MULTILINE)
_TAP_DIRECTIVE = re.compile(r"#\s*(skip|todo)\b", re.IGNORECASE)
_SPEC_POINT = re.compile(r"^\s*[✔✖]\s+(.*?)(?:\s+\([\d.]+ms\))?\s*$", re.MULTILINE)
_SPEC_LINE = re.compile(r"^(?P<indent>[ ]*)(?P<mark>[✔✖])\s+(?P<name>.*?)(?:\s+\([\d.]+ms\))?\s*$")
# node reports a *file* as one passing point when the file declares no test
_TEST_FILE_NAME = re.compile(r"\.(js|cjs|mjs|jsx|ts|cts|mts|tsx)$", re.IGNORECASE)


@dataclass(frozen=True)
class _Point:
    indent: int
    ok: bool
    name: str
    directive: str | None  # "skip", "todo" or None — neither is an executed result


def _tap_points(output: str) -> list[_Point]:
    points = []
    for line in output.splitlines():
        match = _TAP_POINT_LINE.match(line)
        if not match:
            continue
        name = match.group("name")
        directive = None
        found = _TAP_DIRECTIVE.search(name)
        if found:
            directive = found.group(1).lower()
            name = name[: found.start()].strip()
        points.append(
            _Point(len(match.group("indent")), match.group("ok") == "ok", name, directive)
        )
    return points


def _tap_plan(output: str) -> int | None:
    """The last top-level plan line, if any: nested subtests plan themselves."""
    plan = None
    for line in output.splitlines():
        match = _TAP_PLAN.match(line)
        if match and len(match.group("indent")) == 0:
            plan = int(match.group("count"))
    return plan


def _is_node(output: str) -> bool:
    return "# Subtest:" in output or bool(_SPEC_POINT.search(output))


def _spec_points(output: str) -> list[_Point]:
    """Points from node's default reporter, deduplicated by name.

    The spec reporter repeats every failure in a trailing "failing tests:"
    section, so the same name must not be counted twice.
    """
    seen: dict[str, _Point] = {}
    for line in output.splitlines():
        match = _SPEC_LINE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name and name not in seen:
            seen[name] = _Point(len(match.group("indent")), match.group("mark") == "✔", name, None)
    return list(seen.values())


def _no_real_cases(wrappers: list[_Point]) -> TestResult:
    """Only whole-file points were reported: nothing inside them ran."""
    broken = [p for p in wrappers if not p.ok]
    if broken:
        return TestResult(
            Status.ERROR,
            0,
            0,
            "tap",
            f"{len(broken)} test file(s) failed to load without running a test case: "
            + ", ".join(p.name for p in broken),
        )
    named = ", ".join(p.name for p in wrappers)
    return TestResult(
        Status.NO_TESTS,
        0,
        0,
        "tap",
        f"the runner reported only the test file(s) themselves ({named}), so no test "
        "case is defined",
    )


def _is_file_wrapper(point: _Point, test_path: str | None) -> bool:
    """A point standing for a whole file rather than a test case inside it."""
    if _TEST_FILE_NAME.search(point.name):
        return True
    if test_path:
        return point.name in {test_path, test_path.rsplit("/", 1)[-1]}
    return False


def _parse_tap(output: str, returncode: int, test_path: str | None = None) -> TestResult | None:
    bail = _TAP_BAILOUT.search(output)
    if bail:
        return TestResult(
            Status.ERROR, 0, 0, "tap", f"the runner bailed out:{bail.group(1).rstrip()}"
        )
    plan = _tap_plan(output)
    counts = {name: int(value) for name, value in _TAP_COUNT.findall(output)}
    points = _tap_points(output)
    if plan is None and not counts and not points:
        return None

    top = [p for p in points if p.indent == 0] or points

    # node --test flattens a file with no test() call into one passing point
    # named after the file. Those points prove nothing, whether the run covers
    # one file or a whole suite, so they are removed before counting.
    if _is_node(output):
        if points:
            wrappers = [p for p in top if _is_file_wrapper(p, test_path)]
            real = [p for p in top if p not in wrappers]
            if wrappers and real:
                return _tap_verdict(real, plan=None)
            if wrappers:
                return _no_real_cases(wrappers)
        else:
            # the default "spec" reporter prints no TAP points, and repeats
            # failures in a trailing summary, so file wrappers are discounted
            # from its totals rather than recounted
            spec = _spec_points(output)
            wrappers = [p for p in spec if _is_file_wrapper(p, test_path)]
            if wrappers and ("pass" in counts or "fail" in counts):
                broken = [p for p in wrappers if not p.ok]
                executed = counts.get("pass", 0) + counts.get("fail", 0) - len(wrappers)
                failed = counts.get("fail", 0) - len(broken)
                if executed <= 0:
                    return _no_real_cases(wrappers)
                return _verdict("tap", executed, max(failed, 0))

        # totals are node's own summary and outrank counted points, which
        # include nested subtests
        if "pass" in counts or "fail" in counts:
            passed, failed = counts.get("pass", 0), counts.get("fail", 0)
            return _verdict("tap", passed + failed, failed)

    if plan == 0 and not points:
        return TestResult(Status.NO_TESTS, 0, 0, "tap", "the TAP plan declared 0 tests")
    if points:
        return _tap_verdict(top, plan)
    if "pass" in counts or "fail" in counts:
        passed, failed = counts.get("pass", 0), counts.get("fail", 0)
        return _verdict("tap", passed + failed, failed)
    return TestResult(
        Status.ERROR,
        0,
        0,
        "tap",
        f"the TAP report announced totals but no test result (exit {returncode}); "
        "the run is incomplete",
    )


def _tap_verdict(points: list[_Point], plan: int | None) -> TestResult:
    if plan is not None and plan != len(points):
        return TestResult(
            Status.ERROR,
            0,
            0,
            "tap",
            f"the TAP plan announced {plan} tests but {len(points)} were reported — "
            "the run is incomplete",
        )
    ran = [p for p in points if p.directive is None]
    if not ran:
        directives = ", ".join(sorted({p.directive for p in points if p.directive})) or "none"
        return TestResult(
            Status.NO_TESTS,
            0,
            0,
            "tap",
            f"every TAP point was marked {directives}, so no test actually executed",
        )
    failed = sum(1 for p in ran if not p.ok)
    return _verdict("tap", len(ran), failed)


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
# xfailed is deliberately NOT evidence of execution: `xfail(run=False)` reports
# an xfailed test that was never called, so counting it would let a test that
# does nothing satisfy the loop. xpassed always ran.
_PYTEST_EXECUTED = ("passed", "failed", "xpassed")
_PYTEST_INCONCLUSIVE = ("skipped", "xfailed", "deselected")


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
    if executed == 0:
        reported = ", ".join(
            f"{counts[name]} {name}" for name in _PYTEST_INCONCLUSIVE if counts.get(name)
        )
        if reported:
            return TestResult(
                Status.NO_TESTS,
                0,
                0,
                "pytest",
                f"no test produced a result that proves it ran ({reported})",
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
        # `ok  pkg  0.01s` alone is not evidence: it is also printed for a
        # cached result and says nothing about which cases ran.
        if _GO_SKIP.search(output):
            why = "every go test was skipped"
        elif _GO_NO_FILES.search(output) or "no tests to run" in output:
            why = "the package contains no test files"
        elif "(cached)" in output:
            why = "go reused a cached result, so nothing ran — add -count=1 to the test command"
        else:
            why = (
                f"go test exited {returncode} without naming a single test case — run it "
                "with -v -count=1 so each case is reported"
            )
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
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    """Colour codes must never hide a summary line from a parser."""
    return _ANSI.sub("", text)


def interpret(
    command: str, output: str, returncode: int, test_path: str | None = None
) -> TestResult:
    """Classify one finished test command. Never raises."""
    output = strip_ansi(output)
    family = command_family(command)
    order = list(_FAMILY_ORDER.get(family, ()))
    order += [name for name in _PARSERS if name not in order]
    report = None
    for name in order:
        report = _PARSERS[name](output, returncode, test_path)
        if report is not None:
            break

    if report is not None:
        # A green report and a failed command contradict each other: the run may
        # have crashed after its summary, or a wrapper script failed around it.
        # Never resolve that in favour of green.
        if report.status is Status.PASSED and returncode != 0:
            return TestResult(
                Status.ERROR,
                report.executed,
                report.failed,
                report.framework,
                f"{report.framework} reported {report.executed} passing test(s) but the "
                f"command exited {returncode} — the run did not complete cleanly",
            )
        return report

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
