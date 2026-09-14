"""Shared fakes: a scripted Copilot client and a marker-based test command.

FakeClient replays (reply, side_effect) pairs so orchestration tests exercise
the real enforcement logic without any model call. The checker script stands in
for the target repo's test suite: a test file containing PASS passes, one
containing RED passes only once impl.py exists — letting tests control red and
green deterministically.

The checker reports in TAP, the protocol documented for custom test commands,
because the harness now demands evidence that a test really ran: a file with
neither marker is reported as `1..0` (no test case), not as a failure. Given a
directory (or `.`) it runs the whole tree and reports TAP totals, which is what
a full-suite regression gate needs.
"""

import dataclasses
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

    HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
    IMPL = os.path.join(HERE, "impl.py")


    def collect(target):
        # one file, or every marker-bearing file under a directory selector
        if os.path.isdir(target):
            found = []
            for root, _dirs, names in os.walk(target):
                for name in sorted(names):
                    if name in ("checker.py", "impl.py") or not name.endswith(".py"):
                        continue
                    found.append(os.path.join(root, name))
            return found
        return [target]


    def verdict(path):
        # None when the file holds no test case at all
        content = open(path).read()
        if "PASS" in content:
            return True
        if "RED" in content:
            return os.path.exists(IMPL)
        return None


    target = sys.argv[1] if len(sys.argv) > 1 else "."
    cases = [(p, verdict(p)) for p in collect(target)]
    cases = [(p, ok) for p, ok in cases if ok is not None]
    print("checker ran")
    print("TAP version 13")
    if not cases:
        print("1..0 # no test case found in " + target)
        sys.exit(0)
    print("1..%d" % len(cases))
    failed = 0
    for number, (path, ok) in enumerate(cases, start=1):
        if not ok:
            failed += 1
        print(("ok" if ok else "not ok") + " %d - checker %s" % (number, path))
    print("# tests %d" % len(cases))
    print("# pass %d" % (len(cases) - failed))
    print("# fail %d" % failed)
    sys.exit(1 if failed else 0)
""")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "checker.py").write_text(CHECKER_SRC)
    return tmp_path


def _checker_cmd(repo, selector: str) -> str:
    return f"{sys.executable} {repo / 'checker.py'} {selector}"


@pytest.fixture
def cfg(repo):
    # isolate_worktree is off because these tests' side effects write straight
    # into the tmp repo: a run worktree would leave the callbacks editing a
    # directory the runner is not looking at. regression_cmd points the same
    # checker at the whole tree, which reports TAP totals for every marker file.
    options = {
        "repo_dir": repo,
        "test_cmd": _checker_cmd(repo, "{test_path}"),
        "github_tickets": False,
        "tester_retries": 1,
        "coder_retries": 1,
        "max_rounds": 2,
    }
    # tolerated while the fields land: parent owns config.py
    fields = {f.name for f in dataclasses.fields(RunnerConfig)}
    if "isolate_worktree" in fields:
        options["isolate_worktree"] = False
    if "regression_cmd" in fields:
        options["regression_cmd"] = _checker_cmd(repo, ".")
    return RunnerConfig(**options)
