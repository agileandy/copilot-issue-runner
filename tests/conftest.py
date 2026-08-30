"""Shared fakes: a scripted Copilot client and a marker-based test command.

FakeClient replays (reply, side_effect) pairs so orchestration tests exercise
the real enforcement logic without any model call. The checker script stands in
for the target repo's test suite: a test file containing PASS passes, anything
else fails — letting tests control red/green deterministically.
"""

import textwrap

import pytest

from issue_runner.config import RunnerConfig


class FakeClient:
    def __init__(self, script):
        # script: list of (reply, side_effect|None); side_effect() runs before returning
        self.script = list(script)
        self.calls = []

    def run(self, prompt, role, read_only=False, session_name=None):
        self.calls.append({"prompt": prompt, "role": role, "read_only": read_only})
        if not self.script:
            raise AssertionError(f"FakeClient exhausted; unexpected call for role {role}")
        reply, side_effect = self.script.pop(0)
        if side_effect:
            side_effect()
        return reply


@pytest.fixture
def repo(tmp_path):
    checker = tmp_path / "checker.py"
    # Green rules: a test containing PASS is trivially green (stub smell);
    # a test containing RED goes green only once impl.py exists (real TDD flow).
    checker.write_text(
        textwrap.dedent("""\
            import os
            import sys
            content = open(sys.argv[1]).read()
            print("checker ran")
            if "PASS" in content:
                sys.exit(0)
            if "RED" in content and os.path.exists(os.path.join(os.path.dirname(sys.argv[0]), "impl.py")):
                sys.exit(0)
            sys.exit(1)
        """)
    )
    return tmp_path


@pytest.fixture
def cfg(repo):
    return RunnerConfig(
        repo_dir=repo,
        test_cmd=f"python {repo / 'checker.py'} {{test_path}}",
        github_tickets=False,
        tester_retries=1,
        coder_retries=1,
        max_rounds=2,
    )
