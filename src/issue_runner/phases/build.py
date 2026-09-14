"""Build phase: builder.tester and builder.coder steps.

The non-stub rule is enforced by the harness, not by trusting the model:
- the tester's test must be *proved to have executed*: the runner's own report
  must show at least one test case running, and (on the first pass, before any
  implementation exists) failing — a stub, an empty file or a suite that
  collected nothing is bounced back with concrete feedback, costing no
  verifier call;
- the coder must turn that exact test green WITHOUT modifying the test file
  (detected by content hash) — weakening the test to pass is rejected.

Execution evidence comes from `testreport.interpret`, which reads the
framework's own summary. An exit code is never trusted on its own: pytest exits
0 having collected nothing, `go test` exits 0 with no test files, and a missing
binary or an import error exits non-zero without a single test having run.
Those outcomes raise BuildError so the caller can feed them back as a fault to
fix, instead of being mistaken for a red test.
"""

import hashlib
import os
import shlex
import subprocess
from pathlib import Path

from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json
from ..testreport import Status, interpret, strip_ansi
from ..tickets import Ticket

_TEST_TIMEOUT = 600
# Every automatic run must be non-interactive and colour-free: a watch-mode
# runner (vitest, jest --watch) would hang until the timeout, and ANSI codes can
# split the very summary lines the report parser reads. CI=1 is what the JS
# ecosystem checks for "batch run, do not prompt"; stdin is closed so anything
# that still asks a question fails instead of blocking.
_RUN_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "CI": "1",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "NPM_CONFIG_COLOR": "false",
    "PY_COLORS": "0",
    "TERM": "dumb",
}
# language-agnostic emptiness check: cheap pre-filter only — execution evidence
# is the real gate, so this never has to know what an assertion looks like.
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "--", ";", "%")


class BuildError(RuntimeError):
    pass


class TestAlreadyPasses(BuildError):
    """Every tester attempt produced a valid test that passes without new code.

    Strong signal the behaviour already exists — the orchestrator escalates to
    the verifier to arbitrate instead of blocking blindly.
    """

    def __init__(self, message: str, test_path: str):
        super().__init__(message)
        self.test_path = test_path


def resolve_test_path(cfg: RunnerConfig, test_path: str) -> Path:
    """Absolute path of a declared test file, proven to live inside repo_dir.

    The path comes from a model reply, so it is untrusted input: an absolute
    path, a `..` escape or a symlink pointing outside the workspace must never
    be read, hashed or (worse) restored over.
    """
    raw = str(test_path).strip()
    if not raw:
        raise BuildError("no test path was given")
    candidate = Path(raw)
    if candidate.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        raise BuildError(f"test path {raw!r} must be relative to the repository root")
    if ".." in candidate.parts:
        raise BuildError(f"test path {raw!r} must not traverse outside the repository")

    repo = Path(cfg.repo_dir).resolve()
    full = (repo / candidate).resolve()
    if full != repo and repo not in full.parents:
        raise BuildError(f"test path {raw!r} resolves outside the repository at {repo}")
    return full


def _run(cfg: RunnerConfig, command: str, test_path: str | None = None) -> tuple[bool, str]:
    """Run one complete test command and turn its report into a verdict."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise BuildError(f"test command {command!r} could not be parsed: {e}") from e
    if not argv:
        raise BuildError("the test command is empty")

    # No bytecode: the coder may rewrite a same-sized file within the same second
    # as the red check, and stale .pyc reuse would report a phantom failure.
    env = dict(os.environ, **_RUN_ENV)
    # Project-level -q plus the command's -q otherwise hides pytest's result counts.
    env["PYTEST_ADDOPTS"] = f"{env.get('PYTEST_ADDOPTS', '')} --verbosity=0".strip()
    try:
        result = subprocess.run(
            argv,
            cwd=str(cfg.repo_dir),
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        tail = _tail(e.stdout) + _tail(e.stderr)
        raise BuildError(
            f"the test command timed out after {_TEST_TIMEOUT}s: {command}\n"
            "(a watch-mode or interactive runner never finishes — use its single-run "
            f"form)\n{tail}"
        ) from e
    except (OSError, ValueError) as e:
        raise BuildError(f"the test command could not be run: {command}: {e}") from e

    output = strip_ansi(result.stdout + result.stderr).strip()
    report = interpret(command, output, result.returncode, test_path)
    if report.status is Status.PASSED:
        return True, output
    if report.status is Status.FAILED:
        return False, output
    raise BuildError(
        f"no usable test result from `{command}`: {report.detail}\n"
        f"(exit {result.returncode})\n{output[-2000:]}"
    )


def _tail(data) -> str:
    if not data:
        return ""
    text = data.decode(errors="replace") if isinstance(data, bytes) else str(data)
    return text[-1000:]


def run_tests(cfg: RunnerConfig, test_path: str) -> tuple[bool, str]:
    """Run the configured test command against one test file.

    True only when at least one test really executed and all passed; False when
    an executed test failed; BuildError when nothing ran or the runner could not
    produce a result.
    """
    command = cfg.test_cmd.format(test_path=shlex.quote(test_path))
    return _run(cfg, command, test_path)


def run_test_command(cfg: RunnerConfig, command: str) -> tuple[bool, str]:
    """Run an already-formatted command (e.g. the full-suite regression gate)."""
    return _run(cfg, command)


TESTER_PROMPT = """\
You are builder.tester in a strict TDD pipeline working on this repository.

Sub-task: {title}
Description: {description}
The test must verify this single logical assertion: {test_assertion}
Files likely involved: {files_hint}

Write ONE test for this assertion, in the repository's existing test framework
and conventions (look at existing tests first).

HARD RULES:
- The test MUST NOT be a stub: it must exercise real behaviour and contain a
  real assertion. No `pass`, no `assert True`, no TODO placeholders.
- The harness runs your test and requires the runner's own report to show at
  least one test case actually executing. A file the runner skips, cannot
  collect or reports as "no tests" is rejected.
- One logical assertion only (setup/guard code is fine).
- Do NOT write any implementation code — the test is EXPECTED to fail right
  now. That is the point.
- Do not modify any other test.

When done, reply with ONLY this JSON (no prose):
{{"test_path": "<path of the test file relative to the repo root>"}}
{feedback}"""


CODER_PROMPT = """\
You are builder.coder in a strict TDD pipeline working on this repository.

Sub-task: {title}
Description: {description}
A failing test exists at: {test_path}
It verifies: {test_assertion}

Implement the MINIMAL production code that makes this test pass, following the
repository's existing conventions.

HARD RULES:
- Do NOT modify the test file in any way. It is the specification.
- Do not break other existing tests.
- Minimal, focused change — no drive-by refactoring.

When done, reply with ONLY this JSON (no prose):
{{"changed_files": ["<paths you changed>"], "notes": "<one line>"}}
{feedback}"""


def _is_empty_test(content: str) -> bool:
    """Nothing but blank lines and comments — no runner could execute it."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(_COMMENT_PREFIXES):
            return False
    return True


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_hash(path: Path) -> str | None:
    """Hash of the file as it stands, or None if the coder deleted it."""
    try:
        return _hash_file(path)
    except OSError:
        return None


def _restore(path: Path, original: bytes, test_path: str) -> None:
    """Put the tester's exact test back, whatever the coder did to it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    except OSError as e:
        raise BuildError(
            f"builder.coder modified test file {test_path} and it could not be "
            f"restored: {e} — aborting ticket"
        ) from e


def tester_step(
    client,
    cfg: RunnerConfig,
    ticket: Ticket,
    feedback: str | None = None,
    require_red: bool = True,
) -> str:
    extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{feedback}" if feedback else ""
    last_error = "no attempt made"
    already_green_path = None
    for _ in range(cfg.tester_retries + 1):
        prompt = TESTER_PROMPT.format(
            title=ticket.title,
            description=ticket.description,
            test_assertion=ticket.test_assertion,
            files_hint=", ".join(ticket.files_hint) or "explore the repo",
            feedback=extra,
        )
        reply = client.run(prompt, role="builder.tester", session_name=f"tester-t{ticket.id}")
        try:
            test_path = str(extract_json(reply)["test_path"])
        except (JsonExtractError, KeyError, TypeError) as e:
            last_error = f"reply was not the required JSON: {e}"
            extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
            continue

        try:
            full = resolve_test_path(cfg, test_path)
        except BuildError as e:
            already_green_path = None
            last_error = str(e)
            extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
            continue

        if not full.is_file():
            already_green_path = None
            last_error = f"declared test file {test_path} does not exist"
        elif _is_empty_test(full.read_text(errors="replace")):
            already_green_path = None
            last_error = (
                f"test {test_path} contains only comments or blank lines — it is a stub; "
                "write a real test case that exercises the behaviour"
            )
        else:
            try:
                passed, output = run_tests(cfg, test_path)
            except BuildError as e:
                already_green_path = None
                last_error = f"test {test_path} did not actually run, so it proves nothing: {e}"
                extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
                continue
            if require_red and passed:
                already_green_path = test_path
                last_error = (
                    f"test {test_path} already passes with no implementation — "
                    "it does not exercise the new behaviour and must fail first. "
                    f"Test output:\n{output[-1500:]}"
                )
            else:
                return test_path
        extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
    if already_green_path:
        raise TestAlreadyPasses(
            f"ticket {ticket.id}: every candidate test passes without new code — "
            "the behaviour may already exist",
            already_green_path,
        )
    raise BuildError(f"builder.tester failed for ticket {ticket.id}: {last_error}")


def coder_step(
    client,
    cfg: RunnerConfig,
    ticket: Ticket,
    test_path: str,
    feedback: str | None = None,
) -> None:
    full = resolve_test_path(cfg, test_path)
    if not full.is_file():
        raise BuildError(f"test file {test_path} is missing before builder.coder ran")
    # snapshot the spec in memory: it was written by the tester during THIS run
    # and is not committed until the verifier passes, so git cannot restore it
    original = full.read_bytes()
    test_hash = hashlib.sha256(original).hexdigest()
    extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{feedback}" if feedback else ""
    last_error = "no attempt made"
    for _ in range(cfg.coder_retries + 1):
        prompt = CODER_PROMPT.format(
            title=ticket.title,
            description=ticket.description,
            test_path=test_path,
            test_assertion=ticket.test_assertion,
            feedback=extra,
        )
        client.run(prompt, role="builder.coder", session_name=f"coder-t{ticket.id}")

        if _current_hash(full) != test_hash:
            # restore the specification and reject the attempt
            last_error = "you modified the test file — that is forbidden; the test is the spec"
            _restore(full, original, test_path)
        else:
            try:
                passed, output = run_tests(cfg, test_path)
            except BuildError as e:
                # e.g. the implementation broke collection: a fault to fix, not a
                # failing assertion, so say so rather than reporting a red test
                last_error = f"the test could not be executed after your change: {e}"
                extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
                continue
            if passed:
                return
            last_error = f"test still fails. Output:\n{output[-2000:]}"
        extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
    raise BuildError(f"builder.coder failed for ticket {ticket.id}: {last_error}")
