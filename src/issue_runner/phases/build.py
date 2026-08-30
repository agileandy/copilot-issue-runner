"""Build phase: builder.tester and builder.coder steps.

The non-stub rule is enforced by the harness, not by trusting the model:
- the tester's test must contain a real assertion, and (on the first pass,
  before any implementation exists) must FAIL when run — a stub or trivially
  green test is bounced back with concrete feedback, costing no verifier call;
- the coder must turn that exact test green WITHOUT modifying the test file
  (detected by content hash) — weakening the test to pass is rejected.
"""

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

from ..config import RunnerConfig
from ..jsonx import JsonExtractError, extract_json
from ..tickets import Ticket

# a "real" test asserts something: bare assert, unittest-style, or pytest.raises
_ASSERTION_RE = re.compile(r"\bassert\b|\bself\.assert\w+\(|pytest\.raises\(")


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


def run_tests(cfg: RunnerConfig, test_path: str) -> tuple[bool, str]:
    cmd = shlex.split(cfg.test_cmd.format(test_path=shlex.quote(test_path)))
    # No bytecode: the coder may rewrite a same-sized file within the same second
    # as the red check, and stale .pyc reuse would report a phantom failure.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        cmd,
        cwd=str(cfg.repo_dir),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=env,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


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


def _has_assertion(content: str) -> bool:
    return bool(_ASSERTION_RE.search(content))


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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

        full = Path(cfg.repo_dir) / test_path
        if not full.is_file():
            already_green_path = None
            last_error = f"declared test file {test_path} does not exist"
        elif not _has_assertion(full.read_text()):
            already_green_path = None
            last_error = f"test {test_path} contains no real assertion — it is a stub"
        else:
            passed, output = run_tests(cfg, test_path)
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
    full = Path(cfg.repo_dir) / test_path
    test_hash = _hash_file(full)
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

        if _hash_file(full) != test_hash:
            # restore the specification and reject the attempt
            last_error = "you modified the test file — that is forbidden; the test is the spec"
            subprocess.run(
                ["git", "checkout", "--", test_path],
                cwd=str(cfg.repo_dir),
                capture_output=True,
                check=False,
            )
            if _hash_file(full) != test_hash:
                raise BuildError(
                    f"builder.coder tampered with test file {test_path} and it could "
                    "not be restored from git — aborting ticket"
                )
        else:
            passed, output = run_tests(cfg, test_path)
            if passed:
                return
            last_error = f"test still fails. Output:\n{output[-2000:]}"
        extra = f"\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):\n{last_error}"
    raise BuildError(f"builder.coder failed for ticket {ticket.id}: {last_error}")
