"""The real CLI clones the seed through gh and runs only the clone.

Only the external gh/copilot executables are replaced, so no GitHub writes or
model credits are needed to exercise command routing and persisted run state.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from issue_runner.demo import setup_demo
from tests.hardening_support import git

REPO = "agileandy/copilot-issue-runner"
PROJECT = Path(__file__).resolve().parents[1]
SEED = {
    "number": 54,
    "title": "[DEMO] Verifier hand-back",
    "body": "Only issue 54 is permanent. On a clone, implement the demo without cloning again.\n"
    "<!-- issue-runner-permanent-demo-seed:v2 -->\n",
    "url": f"https://github.com/{REPO}/issues/54",
}

GH_STUB = """\
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_GH_DATA"])
data = json.loads(path.read_text())
args = sys.argv[1:]
data["commands"].append(args)
path.write_text(json.dumps(data))
if args[:2] == ["issue", "view"]:
    number = int(args[2])
    issue = next(i for i in [data["seed"], *data["clones"]] if i["number"] == number)
    print(json.dumps(issue))
elif args[:2] == ["issue", "create"]:
    if data.get("fail_create"):
        print("creation refused", file=sys.stderr)
        sys.exit(1)
    title = args[args.index("--title") + 1]
    bucket = "subissues" if title.startswith("[#") else "clones"
    number = 100 + len(data["clones"]) + len(data.get("subissues", []))
    issue = {
        "number": number,
        "title": title,
        "body": args[args.index("--body") + 1],
        "url": "https://github.com/agileandy/copilot-issue-runner/issues/" + str(number),
    }
    data.setdefault(bucket, []).append(issue)
    path.write_text(json.dumps(data))
    print(issue["url"])
elif args[:2] == ["issue", "list"]:
    prefix = args[args.index("--search") + 1].split(" in:title")[0]
    matches = [
        {"number": i["number"], "title": i["title"]}
        for i in data.get("subissues", [])
        if i["title"].startswith(prefix)
    ]
    print(json.dumps(matches))
elif args[:2] == ["issue", "delete"]:
    if data.get("refuse_delete"):
        print("GraphQL: Viewer not authorized to delete (deleteIssue)", file=sys.stderr)
        sys.exit(1)
    number = int(next(a for a in args[2:] if a.isdigit()))
    for bucket in ("clones", "subissues"):
        data[bucket] = [i for i in data.get(bucket, []) if i["number"] != number]
    data["deleted"] = data.get("deleted", []) + [number]
    path.write_text(json.dumps(data))
elif args[:2] in (["issue", "close"], ["issue", "comment"]):
    if data.get("refuse_close"):
        print("close refused", file=sys.stderr)
        sys.exit(1)
else:
    raise SystemExit("unexpected GitHub write: " + repr(args))
"""


@pytest.fixture
def live_demo(tmp_path):
    fixture = setup_demo(tmp_path / "target")
    git(fixture.repo_dir, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    data = tmp_path / "github.json"
    data.write_text(json.dumps({"seed": SEED, "clones": [], "commands": []}))
    tools = tmp_path / "tools"
    tools.mkdir()
    scripts = {
        "gh": GH_STUB,
        "copilot": "from issue_runner.demo.responder import main\nraise SystemExit(main())\n",
        "fake-copilot": "raise SystemExit('demo must not auto-select fake-copilot')\n",
    }
    for name, code in scripts.items():
        path = tools / name
        path.write_text(f"#!{sys.executable}\n{code}")
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        PYTHONPATH=os.pathsep.join([str(PROJECT / "src"), str(PROJECT)]),
        PYTHONDONTWRITEBYTECODE="1",
        ISSUE_RUNNER_DEMO_DELAY="0",
        FAKE_GH_DATA=str(data),
        TMPDIR=str(tmp_path),
    )
    return fixture, data, env


def run_cli(live_demo, *args):
    fixture, _, env = live_demo
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "issue_runner.cli",
            *args,
            "--dir",
            str(fixture.repo_dir),
            "--test-cmd",
            fixture.test_cmd,
            "--regression-cmd",
            fixture.test_cmd.format(test_path="tests"),
        ],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


def test_demo_clones_exact_content_and_runs_only_the_clone(live_demo):
    fixture, data_path, _ = live_demo
    source_head = git(fixture.repo_dir, "rev-parse", "HEAD")
    result = run_cli(live_demo, "--demo", "--visual")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(data_path.read_text())
    assert len(data["clones"]) == 1
    clone = data["clones"][0]
    assert (clone["title"], clone["body"]) == (SEED["title"], SEED["body"])
    assert clone["number"] != 54
    state_dir = fixture.repo_dir / ".issue-runner"
    assert not (state_dir / "issue-54.json").exists()
    state = json.loads((state_dir / f"issue-{clone['number']}.json").read_text())
    assert state["issue_ref"] == str(clone["number"])
    assert state["branch"].startswith(f"issue-{clone['number']}-")
    assert all(ticket["status"] == "done" for ticket in state["tickets"])
    assert all(ticket["github_issue"] is not None for ticket in state["tickets"])
    assert git(fixture.repo_dir, "rev-parse", "HEAD") == source_head
    assert git(fixture.repo_dir, "branch", "--show-current") == "main"
    assert [args[:2] for args in data["commands"]][:2] == [["issue", "view"], ["issue", "create"]]
    assert clone["url"] in result.stdout
    assert "real model calls" in result.stdout.lower()


def test_demo_mirrors_each_ticket_as_a_subissue_on_the_clone(live_demo):
    fixture, data_path, _ = live_demo
    result = run_cli(live_demo, "--demo")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(data_path.read_text())
    clone = data["clones"][0]
    state = json.loads(
        (fixture.repo_dir / ".issue-runner" / f"issue-{clone['number']}.json").read_text()
    )
    subissues = data["subissues"]
    assert subissues, "the demo should mirror tickets as sub-issues"
    assert [i["number"] for i in subissues] == [t["github_issue"] for t in state["tickets"]]
    assert all(i["title"].startswith(f"[#{clone['number']}] ") for i in subissues)
    assert all(f"Part of #{clone['number']}." in i["body"] for i in subissues)
    closed = [
        next(a for a in args[2:] if a.isdigit())
        for args in data["commands"]
        if args[:2] == ["issue", "close"]
    ]
    assert sorted(closed) == sorted(str(i["number"]) for i in subissues)


def test_demo_subissues_never_attach_to_the_seed(live_demo):
    result = run_cli(live_demo, "--demo")
    assert result.returncode == 0, result.stdout + result.stderr
    _, data_path, _ = live_demo
    data = json.loads(data_path.read_text())
    assert data["subissues"], "the demo performs sub-issue writes"
    assert all(not i["title"].startswith("[#54] ") for i in data["subissues"])
    assert all("Part of #54." not in i["body"] for i in data["subissues"])
    targeted = [
        args
        for args in data["commands"]
        if args[:2] in (["issue", "close"], ["issue", "comment"], ["issue", "delete"])
    ]
    assert all(next(a for a in args[2:] if a.isdigit()) != "54" for args in targeted)


def test_demo_resume_command_keeps_subissue_mirroring_on(live_demo):
    result = run_cli(live_demo, "--demo", "--plan-only")
    assert result.returncode == 0, result.stdout + result.stderr
    resume = next(
        line for line in result.stdout.splitlines() if line.startswith("Resume this clone:")
    )
    assert "--no-github-tickets" not in resume
    assert "--no-pr" in resume


def test_no_github_tickets_still_disables_demo_mirroring(live_demo):
    result = run_cli(live_demo, "--demo", "--no-github-tickets")
    assert result.returncode == 0, result.stdout + result.stderr
    _, data_path, _ = live_demo
    data = json.loads(data_path.read_text())
    assert len(data["clones"]) == 1
    assert [args[:2] for args in data["commands"]] == [["issue", "view"], ["issue", "create"]]


def test_demo_clean_deletes_the_clone_and_its_subissues(live_demo):
    assert run_cli(live_demo, "--demo").returncode == 0
    fixture, data_path, _ = live_demo
    before = json.loads(data_path.read_text())
    clone = before["clones"][0]
    state_dir = fixture.repo_dir / ".issue-runner"
    state_file = state_dir / f"issue-{clone['number']}.json"
    branch = json.loads(state_file.read_text())["branch"]
    worktree = state_dir / "worktrees" / str(clone["number"])
    assert worktree.is_dir()
    assert branch in git(fixture.repo_dir, "branch", "--list", branch)

    result = run_cli(live_demo, "--demo-clean", str(clone["number"]))
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(data_path.read_text())
    assert after["clones"] == []
    assert after["subissues"] == []
    assert sorted(after["deleted"]) == sorted(
        [clone["number"], *[i["number"] for i in before["subissues"]]]
    )
    assert str(clone["number"]) in result.stdout
    assert not state_file.exists()
    assert not (state_dir / f"usage-issue-{clone['number']}.json").exists()
    assert not worktree.exists()
    assert git(fixture.repo_dir, "branch", "--list", branch) == ""
    assert git(fixture.repo_dir, "branch", "--show-current") == "main"
    assert git(fixture.repo_dir, "status", "--porcelain") == ""


def test_demo_clean_leaves_unrelated_branches_and_state_alone(live_demo):
    assert run_cli(live_demo, "--demo").returncode == 0
    fixture, data_path, _ = live_demo
    clone = json.loads(data_path.read_text())["clones"][0]
    git(fixture.repo_dir, "branch", "keep-me")
    other = fixture.repo_dir / ".issue-runner" / "issue-999.json"
    other.write_text("{}")

    assert run_cli(live_demo, "--demo-clean", str(clone["number"])).returncode == 0
    assert "keep-me" in git(fixture.repo_dir, "branch", "--list", "keep-me")
    assert other.exists()


def test_demo_clean_refuses_the_permanent_seed(live_demo):
    result = run_cli(live_demo, "--demo-clean", "54")
    assert result.returncode == 2
    assert "54" in result.stderr
    _, data_path, _ = live_demo
    assert json.loads(data_path.read_text()).get("deleted", []) == []


def test_each_demo_invocation_creates_a_new_clone(live_demo):
    for _ in range(2):
        result = run_cli(live_demo, "--demo", "--plan-only")
        assert result.returncode == 0, result.stdout + result.stderr
    fixture, data_path, _ = live_demo
    clones = json.loads(data_path.read_text())["clones"]
    numbers = [c["number"] for c in clones]
    assert len(numbers) == 2 and len(set(numbers)) == 2 and 54 not in numbers
    assert all(
        (fixture.repo_dir / ".issue-runner" / f"issue-{c['number']}.json").exists() for c in clones
    )


def test_demo_dry_run_does_not_clone_or_start_a_model(live_demo):
    fixture, data_path, _ = live_demo
    result = run_cli(live_demo, "--demo", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(data_path.read_text())
    assert data["clones"] == []
    assert [args[:2] for args in data["commands"]] == [["issue", "view"]]
    assert "would clone" in result.stdout.lower()
    assert "GitHub issue #<new clone>:" in result.stdout
    assert "GitHub issue #54:" not in result.stdout
    assert not list((fixture.repo_dir / ".issue-runner").glob("issue-*.json"))


def test_seed_cannot_be_run_directly(live_demo):
    fixture, _, _ = live_demo
    result = run_cli(live_demo, "54")
    assert result.returncode == 2
    assert "--demo" in result.stderr
    assert not (fixture.repo_dir / ".issue-runner" / "issue-54.json").exists()


def test_dirty_demo_target_is_rejected_before_cloning(live_demo):
    fixture, data_path, _ = live_demo
    (fixture.repo_dir / "user-work.txt").write_text("preserve this")
    result = run_cli(live_demo, "--demo")
    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert json.loads(data_path.read_text())["clones"] == []


def test_clone_failure_never_runs_the_seed(live_demo):
    fixture, data_path, _ = live_demo
    data = json.loads(data_path.read_text())
    data["fail_create"] = True
    data_path.write_text(json.dumps(data))
    result = run_cli(live_demo, "--demo")
    assert result.returncode == 2
    assert "creation refused" in result.stderr
    assert not list((fixture.repo_dir / ".issue-runner").glob("issue-*.json"))


def test_demo_refuses_a_different_target_repository(live_demo):
    fixture, data_path, _ = live_demo
    git(fixture.repo_dir, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    result = run_cli(live_demo, "--demo")
    assert result.returncode == 2
    assert REPO in result.stderr
    assert json.loads(data_path.read_text())["clones"] == []


def test_a_clone_can_resume_without_cloning_again(live_demo):
    fixture, data_path, _ = live_demo
    planned = run_cli(live_demo, "--demo", "--plan-only")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    resumed = run_cli(
        live_demo, "100", "--no-github-tickets", "--no-pr", "--copilot-cmd", "copilot"
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(json.loads(data_path.read_text())["clones"]) == 1
    state = json.loads((fixture.repo_dir / ".issue-runner" / "issue-100.json").read_text())
    assert all(t["status"] == "done" for t in state["tickets"])


def test_core_pipeline_also_refuses_the_permanent_seed(tmp_path):
    from issue_runner.config import RunnerConfig
    from issue_runner.orchestrator import run_issue
    from issue_runner.phases.devops import DevopsError

    with pytest.raises(DevopsError, match="permanent seed"):
        run_issue(RunnerConfig(repo_dir=tmp_path, repo=REPO), object(), SEED)
    assert not (tmp_path / ".issue-runner").exists()


def test_resume_command_keeps_options_but_does_not_repeat_demo_or_plan_only(live_demo):
    result = run_cli(
        live_demo, "--demo", "--plan-only", "--model", "test-model", "--max-run-credits", "60"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("Resume this clone:")
    )
    args = shlex.split(line.removeprefix("Resume this clone: "))
    assert args[:2] == ["gh-runner", "100"]
    assert "--demo" not in args and "--plan-only" not in args
    assert args[args.index("--model") + 1] == "test-model"
    assert args[args.index("--max-run-credits") + 1] == "60"
    assert args[args.index("--copilot-cmd") + 1] == "copilot"
    assert "--no-pr" in args and "--no-github-tickets" not in args


def test_demo_clean_closes_issues_when_the_token_cannot_delete(live_demo):
    assert run_cli(live_demo, "--demo").returncode == 0
    fixture, data_path, _ = live_demo
    data = json.loads(data_path.read_text())
    data["refuse_delete"] = True
    data_path.write_text(json.dumps(data))
    clone = data["clones"][0]
    worktree = fixture.repo_dir / ".issue-runner" / "worktrees" / str(clone["number"])
    assert worktree.is_dir()

    result = run_cli(live_demo, "--demo-clean", str(clone["number"]))
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(data_path.read_text())
    closed = [
        next(a for a in args[2:] if a.isdigit())
        for args in after["commands"]
        if args[:2] == ["issue", "close"]
    ]
    assert str(clone["number"]) in closed
    assert all(str(i["number"]) in closed for i in data["subissues"])
    assert "closed" in result.stdout.lower()
    assert not worktree.exists()


def test_demo_clean_removes_local_work_even_if_github_fails_entirely(live_demo):
    assert run_cli(live_demo, "--demo").returncode == 0
    fixture, data_path, _ = live_demo
    data = json.loads(data_path.read_text())
    data["refuse_delete"] = True
    data["refuse_close"] = True
    data_path.write_text(json.dumps(data))
    clone = data["clones"][0]
    state_file = fixture.repo_dir / ".issue-runner" / f"issue-{clone['number']}.json"
    branch = json.loads(state_file.read_text())["branch"]

    result = run_cli(live_demo, "--demo-clean", str(clone["number"]))
    assert result.returncode == 1
    assert not state_file.exists()
    assert git(fixture.repo_dir, "branch", "--list", branch) == ""
    assert "could not" in result.stderr.lower() or "could not" in result.stdout.lower()
