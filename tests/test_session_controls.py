"""Exercise detach, reattach and stop through a real terminal and offline child."""

import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from issue_runner.demo import setup_demo

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX pseudo-terminal")
PROJECT = Path(__file__).resolve().parents[1]


class Terminal:
    def __init__(self, proc, fd, repo):
        self.proc, self.fd, self.repo = proc, fd, repo
        self.output = b""

    def read(self):
        if select.select([self.fd], [], [], 0.05)[0]:
            try:
                self.output += os.read(self.fd, 65536)
            except OSError as e:
                if e.errno != errno.EIO:
                    raise

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            if predicate():
                return
        pytest.fail(f"terminal condition was not reached:\n{self.output[-5000:]!r}")

    def press(self, keys):
        self.output = b""
        os.write(self.fd, keys)

    def expect(self, text):
        self.until(lambda: text in self.output)

    def release_call(self):
        (self.repo / ".issue-runner" / "release-call").touch()

    def wait_exit(self):
        self.until(lambda: self.proc.poll() is not None)
        return self.proc.returncode


@contextmanager
def running_cli(tmp_path, *, visual=True, pause_role="planner"):
    import fcntl
    import pty
    import struct
    import termios

    demo = setup_demo(tmp_path / "demo")
    fake = tmp_path / "controlled-copilot"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time\n"
        "from issue_runner.demo.responder import parse_argv, respond, role_of\n"
        "prompt, repo, stream = parse_argv(__import__('sys').argv[1:])\n"
        "with (repo / '.issue-runner' / 'call-pids').open('a') as log:\n"
        "    log.write(str(os.getpid()) + '\\n')\n"
        "reply, _, _ = respond(prompt, repo)\n"
        "if role_of(prompt) == os.environ['PAUSE_ROLE']:\n"
        "    (repo / '.issue-runner' / 'call-paused').touch()\n"
        "    while not (repo / '.issue-runner' / 'release-call').exists():\n"
        "        time.sleep(0.02)\n"
        "print(json.dumps({'type':'assistant.message','data':{'content':reply}}))\n"
        "print(json.dumps({'type':'model.model_call_success','data':{\n"
        "    'responseUsage':{'prompt_tokens':1,'completion_tokens':1},\n"
        "    'copilotUsage':{'total_nano_aiu':0}}}))\n"
    )
    fake.chmod(0o755)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(PROJECT / "src"), str(PROJECT)]),
        PYTHONDONTWRITEBYTECODE="1",
        TERM="xterm-256color",
        PAUSE_ROLE=pause_role,
    )
    args = [
        sys.executable,
        "-c",
        (
            "import fcntl,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); "
            "from issue_runner.cli import main; raise SystemExit(main())"
        ),
        "--issue-file",
        str(demo.issue_file),
        "--dir",
        str(demo.repo_dir),
        "--copilot-cmd",
        str(fake),
        "--test-cmd",
        demo.test_cmd,
        "--regression-cmd",
        demo.test_cmd.format(test_path="tests"),
        "--no-pr",
        "--no-github-tickets",
        "--in-place",
    ]
    if visual:
        args.append("--visual")
    proc = subprocess.Popen(
        args, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    os.close(slave)
    terminal = Terminal(proc, master, demo.repo_dir)
    try:
        terminal.until(lambda: (demo.repo_dir / ".issue-runner" / "call-paused").exists())
        yield terminal
    finally:
        pids = demo.repo_dir / ".issue-runner" / "call-pids"
        if pids.exists():
            for pid in map(int, pids.read_text().splitlines()):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        os.close(master)


def test_detach_then_reattach_in_the_same_terminal(tmp_path):
    with running_cli(tmp_path) as terminal:
        terminal.press(b"q")
        terminal.expect(b"display detached")
        terminal.press(b"r\n")
        terminal.expect(b"\x1b[?1049h")
        terminal.expect(b"planner")
        assert len((terminal.repo / ".issue-runner" / "call-pids").read_text().splitlines()) == 1


@pytest.mark.parametrize("mode", ["visual", "detached", "plain"])
def test_ctrl_c_reports_stopping_and_exits_at_the_next_call_boundary(tmp_path, mode):
    with running_cli(tmp_path, visual=mode != "plain") as terminal:
        if mode == "detached":
            terminal.press(b"q")
            terminal.expect(b"display detached")
        terminal.press(b"\x03")
        terminal.expect(b"Stopping and cleaning up")
        terminal.release_call()
        assert terminal.wait_exit() == 130
        assert b"Traceback" not in terminal.output
        state_file = next((terminal.repo / ".issue-runner").glob("issue-*.json"))
        state = json.loads(state_file.read_text())
        assert state["tickets"]
        assert all(ticket["status"] == "pending" for ticket in state["tickets"])
        usage_file = next((terminal.repo / ".issue-runner").glob("usage-issue-*.json"))
        assert json.loads(usage_file.read_text())["totals"]["calls"] == 1
        assert len((terminal.repo / ".issue-runner" / "call-pids").read_text().splitlines()) == 1


def test_second_ctrl_c_interrupts_the_current_child_and_exits(tmp_path):
    with running_cli(tmp_path) as terminal:
        terminal.press(b"\x03")
        terminal.expect(b"Stopping and cleaning up")
        terminal.press(b"\x03")
        assert terminal.wait_exit() == 130
        for pid in map(
            int, (terminal.repo / ".issue-runner" / "call-pids").read_text().splitlines()
        ):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_stop_after_coder_saves_the_verifier_checkpoint_and_releases_locks(tmp_path):
    from issue_runner.phases.devops import repository_lock, state_lock

    with running_cli(tmp_path, visual=False, pause_role="coder") as terminal:
        terminal.press(b"\x03")
        terminal.expect(b"Stopping and cleaning up")
        terminal.release_call()
        assert terminal.wait_exit() == 130
        state_file = next((terminal.repo / ".issue-runner").glob("issue-*.json"))
        ticket = json.loads(state_file.read_text())["tickets"][0]
        assert (ticket["status"], ticket["phase"]) == ("pending", "verifier")
        assert ticket["test_hash"] and ticket["test_snapshot"]
        assert len((terminal.repo / ".issue-runner" / "call-pids").read_text().splitlines()) == 3
        with repository_lock(terminal.repo), state_lock(terminal.repo / ".issue-runner"):
            assert state_file.is_file()
