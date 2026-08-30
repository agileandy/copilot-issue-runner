import json
import subprocess

from issue_runner.github_io import create_subissue, fetch_issue, issue_from_file
from issue_runner.tickets import Ticket


class FakeRun:
    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def test_fetch_issue_uses_gh_json():
    payload = {"number": 17, "title": "Add subtract", "body": "body", "url": "https://x/17"}
    run = FakeRun(stdout=json.dumps(payload))
    issue = fetch_issue("17", repo="owner/repo", run=run)
    assert issue["number"] == 17
    argv = run.calls[0]
    assert argv[:3] == ["gh", "issue", "view"]
    assert "owner/repo" in argv


def test_issue_from_file(tmp_path):
    f = tmp_path / "issue.md"
    f.write_text("# Add subtract\n\nWe need a subtract function.\n")
    issue = issue_from_file(f)
    assert issue["title"] == "Add subtract"
    assert "subtract function" in issue["body"]
    assert issue["number"] == 0


def test_create_subissue_parses_number():
    run = FakeRun(stdout="https://github.com/owner/repo/issues/42\n")
    t = Ticket(id=1, title="sub", description="d", test_assertion="a == 1")
    number = create_subissue("owner/repo", parent_number=17, ticket=t, run=run)
    assert number == 42
    argv = run.calls[0]
    assert argv[:3] == ["gh", "issue", "create"]
    body = argv[argv.index("--body") + 1]
    assert "#17" in body and "a == 1" in body
