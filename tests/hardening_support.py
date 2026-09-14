import json
import subprocess

from issue_runner.config import RunnerConfig
from issue_runner.copilot import CopilotClient
from issue_runner.demo import setup_demo
from issue_runner.tickets import TicketStore

ISSUE = {"number": 17, "title": "Add statistics", "body": "Implement mean.", "url": ""}


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def sandbox(tmp_path, *, isolated=False, **overrides):
    env = setup_demo(tmp_path / "target")
    cfg = RunnerConfig(
        repo_dir=env.repo_dir,
        copilot_cmd=str(env.copilot_cmd),
        test_cmd=env.test_cmd,
        regression_cmd=env.test_cmd.format(test_path="tests"),
        isolate_worktree=isolated,
        github_tickets=False,
        open_pr=False,
        **overrides,
    )
    return env, cfg


def load_store(env):
    store = TicketStore(env.repo_dir / ".issue-runner", issue_ref="17")
    assert store.load()
    return store


class Interrupted(RuntimeError):
    pass


class DemoClient(CopilotClient):
    """Real offline subprocess transport, with a one-ticket plan and a crash boundary."""

    def __init__(self, cfg, *, before=None, one_ticket=True):
        super().__init__(cfg)
        self.before = before
        self.one_ticket = one_ticket
        self.roles = []

    def run(self, prompt, role, **kwargs):
        if role == self.before:
            raise Interrupted(role)
        self.roles.append(role)
        reply = super().run(prompt, role=role, **kwargs)
        if role == "planner" and self.one_ticket:
            data = json.loads(reply)
            data["tickets"] = data["tickets"][:1]
            return json.dumps(data)
        return reply
