"""Shared fakes: a scripted Copilot client and a marker-based test command.

FakeClient replays (reply, side_effect) pairs so orchestration tests exercise
the real enforcement logic without any model call. The checker script stands in
for the target repo's test suite: a test file containing PASS passes, one
containing RED passes only once impl.py exists — letting tests control red and
green deterministically.

The checker reports in TAP, the protocol documented for custom test commands,
because the harness now demands evidence that a test really ran: a file with
neither marker is reported as `1..0` (no test case), not as a failure.
"""

import sys
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


CHECKER_SRC = textwrap.dedent("""\
    import os
    import sys

    content = open(sys.argv[1]).read()
    impl = os.path.join(os.path.dirname(sys.argv[0]), "impl.py")
    print("checker ran")
    print("TAP version 13")
    if "PASS" in content:
        green = True
    elif "RED" in content:
        green = os.path.exists(impl)
    else:
        print("1..0 # no test case found in " + sys.argv[1])
        sys.exit(0)
    print("1..1")
    print(("ok" if green else "not ok") + " 1 - checker")
    sys.exit(0 if green else 1)
""")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "checker.py").write_text(CHECKER_SRC)
    return tmp_path


@pytest.fixture
def cfg(repo):
    return RunnerConfig(
        repo_dir=repo,
        test_cmd=f"{sys.executable} {repo / 'checker.py'} {{test_path}}",
        github_tickets=False,
        tester_retries=1,
        coder_retries=1,
        max_rounds=2,
    )
