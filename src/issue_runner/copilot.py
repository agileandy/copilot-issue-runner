"""Subprocess wrapper around the GitHub Copilot CLI (non-interactive mode).

Every model call goes through CopilotClient.run(). Two execution paths:

- legacy (no event bus): `-s` text mode via an injectable `runner` callable —
  tests inject fakes here, behaviour identical to the original client.
- streaming (event bus present): `--output-format json` via Popen; copilot's
  JSONL events are parsed line-by-line and forwarded on the bus as
  `agent_output` chunks (reasoning, tool activity, answer deltas), the final
  reply is the last non-blank `assistant.message` content, and token usage from
  `model.model_call_success` rides on `agent_call_finished`.

`git push` is denied unconditionally: the runner commits on a branch, pushing
is a human decision. Deny rules take precedence over --allow-all-tools.
"""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from .budget import RunBudget
from .config import RunnerConfig
from .events import emit

ALWAYS_DENY = ("shell(git push)",)
READ_ONLY_DENY = ("write", "shell(git:*)")


class CopilotError(RuntimeError):
    pass


class CopilotClient:
    def __init__(self, config: RunnerConfig, runner=subprocess.run, popen=subprocess.Popen):
        self.config = config
        self.runner = runner
        self.popen = popen
        self.budget = RunBudget(limit=config.max_run_credits, per_call=config.max_ai_credits or 1)

    def run(
        self,
        prompt: str,
        role: str,
        read_only: bool = False,
        session_name: str | None = None,
    ) -> str:
        stream = self.config.events is not None
        self.budget.check()  # never start a call the run budget cannot afford
        argv = self._build_argv(prompt, role, read_only, session_name, stream=stream)
        role_cfg = self.config.role(role)
        emit(
            self.config.events,
            "agent_call_started",
            role=role,
            session=session_name,
            model=role_cfg.model,
        )
        started = time.monotonic()
        self.budget.charge()
        if stream:
            returncode, reply, detail, usage = self._stream_call(argv, role, session_name)
        else:
            returncode, reply, detail, usage = self._plain_call(argv, role)
        emit(
            self.config.events,
            "agent_call_finished",
            role=role,
            session=session_name,
            elapsed=round(time.monotonic() - started, 1),
            ok=returncode == 0,
            usage=usage,
        )
        if returncode != 0:
            raise CopilotError(
                f"copilot exited {returncode} for role {role}: {detail.strip()[:500]}"
            )
        return reply

    def _plain_call(self, argv, role):
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                cwd=str(self.config.repo_dir),
            )
        except subprocess.TimeoutExpired as e:
            raise CopilotError(
                f"copilot timed out after {self.config.timeout}s for role {role}"
            ) from e
        return (
            result.returncode,
            result.stdout.strip(),
            result.stderr or result.stdout or "",
            None,
        )

    def _stream_call(self, argv, role, session_name):
        proc = self.popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.config.repo_dir),
        )
        lines: queue.Queue = queue.Queue()

        def _read():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()

        deadline = time.monotonic() + self.config.timeout
        final_message = ""
        failure_detail = ""
        usage = None
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise CopilotError(
                    f"copilot timed out after {self.config.timeout}s for role {role}"
                )
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            chunk, message, fail, call_usage = _parse_event_line(line)
            if chunk:
                emit(
                    self.config.events,
                    "agent_output",
                    role=role,
                    session=session_name,
                    chunk=chunk,
                )
            if message and message.strip():
                final_message = message
            if fail:
                failure_detail = fail
            if call_usage:
                usage = call_usage

        returncode = proc.wait(timeout=30)
        stderr_tail = proc.stderr.read() if proc.stderr else ""
        detail = failure_detail or stderr_tail or final_message
        return returncode, final_message.strip(), detail, usage

    def _build_argv(self, prompt, role, read_only, session_name, stream=False):
        cfg = self.config
        argv = [cfg.copilot_cmd, "-p", prompt]
        argv += ["--output-format", "json"] if stream else ["-s"]
        argv += [
            "--allow-all-tools",
            "--no-ask-user",
            "--no-auto-update",
            "--no-color",
            "--log-level",
            "error",
            "-C",
            str(Path(cfg.repo_dir)),
        ]
        # cfg.visual is runner-side rendering only — it must never alter this argv
        deny = list(ALWAYS_DENY) + (list(READ_ONLY_DENY) if read_only else [])
        for tool in deny:
            argv += ["--deny-tool", tool]

        role_cfg = cfg.role(role)
        if role_cfg.model:
            argv += ["--model", role_cfg.model]
        if role_cfg.effort:
            argv += ["--effort", role_cfg.effort]
        if cfg.max_ai_credits:
            argv += ["--max-ai-credits", str(cfg.max_ai_credits)]
        if session_name:
            argv += ["--name", session_name]
        return argv


def _parse_event_line(line: str):
    """Map one copilot JSONL event to (output_chunk, message, failure, usage)."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None, None, None, None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None, None
    kind = event.get("type", "")
    data = event.get("data") or {}

    if kind == "assistant.reasoning_delta":
        return data.get("deltaContent"), None, None, None
    if kind == "assistant.message_delta":
        return data.get("deltaContent"), None, None, None
    if kind == "tool.execution_start":
        name = data.get("toolName", "tool")
        args = json.dumps(data.get("arguments", {}))[:120]
        return f"\n⚙ {name} {args}\n", None, None, None
    if kind == "assistant.message":
        return None, data.get("content"), None, None
    if kind == "model.model_call_success":
        usage = (data.get("responseChunk") or {}).get("usage") or None
        if usage:
            usage = {k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage}
        return None, None, None, usage
    if kind in ("model.turn_failed", "session.error"):
        return None, None, json.dumps(data)[:500], None
    return None, None, None, None
