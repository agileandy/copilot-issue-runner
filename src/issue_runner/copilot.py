"""Subprocess wrapper around the GitHub Copilot CLI (non-interactive mode).

Every model call goes through CopilotClient.run(). Production — headless and
visual alike — uses one transport: `--output-format json` via Popen, with
copilot's JSONL events parsed line by line. That is the only mode in which
copilot reports tokens and the real credit charge, so `-s` is never used for a
metered run; headless simply has no subscriber, and streaming chunks are only
forwarded when an event bus is attached.

The `runner` constructor argument remains the injection seam for tests: pass
anything other than `subprocess.run` and the client takes the plain `-s` path
with a `CompletedProcess`-shaped return. Nothing in production does that.

Accounting is per invocation, summed over *every* `model.model_call_success`
event. One CLI invocation routinely makes several model calls (tool loops), and
recording only the last one under-reported a run by most of its real cost.
A model success event with no usage payload still counts as a model call with
unknown cost — never as zero.

`git push` is denied unconditionally: the runner commits on a branch, pushing
is a human decision. Deny rules take precedence over --allow-all-tools.
"""

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .budget import RunBudget
from .config import RunnerConfig
from .events import emit
from .usage import UsageLedger, cost_is_complete, mark_incomplete, merge_usage

log = logging.getLogger("issue_runner")

ALWAYS_DENY = ("shell(git push)",)
READ_ONLY_DENY = ("write", "shell(git:*)")

EXIT_GRACE_SECONDS = 30
KILL_GRACE_SECONDS = 5
READER_JOIN_SECONDS = 5
STDERR_TAIL_CHARS = 4000
RAW_STDOUT_LINES = 2000


class CopilotError(RuntimeError):
    pass


class CopilotClient:
    def __init__(self, config: RunnerConfig, runner=subprocess.run, popen=subprocess.Popen):
        self.config = config
        self.runner = runner
        self.popen = popen
        # only an injected runner takes the unmetered plain path; production never does
        self.plain_transport = runner is not subprocess.run
        self.budget = RunBudget(limit=config.max_run_credits, per_call=config.max_ai_credits or 1)
        self.usage = UsageLedger()

    def run(
        self,
        prompt: str,
        role: str,
        read_only: bool = False,
        session_name: str | None = None,
    ) -> str:
        """Run one model call, retrying a blank reply.

        The real Copilot CLI intermittently exits 0 having written nothing to
        stdout. Returning that empty string let callers spend a retry on a
        nonsense parse error, so it is treated here as the transport failure it
        is. Every attempt is still budgeted and accounted — a wasted call is
        real spend.
        """
        attempts = max(0, self.config.empty_reply_retries) + 1
        for attempt in range(1, attempts + 1):
            reply = self._attempt(prompt, role, read_only, session_name)
            if reply:
                return reply
            log.warning(
                "copilot returned an empty reply for role %s (attempt %d/%d)",
                role,
                attempt,
                attempts,
            )
        raise CopilotError(
            f"copilot returned an empty reply for role {role} after {attempts} attempts "
            "(exit 0, no output) — this is a CLI transport failure, not a model refusal"
        )

    def _attempt(
        self,
        prompt: str,
        role: str,
        read_only: bool,
        session_name: str | None,
    ) -> str:
        structured = not self.plain_transport
        self.budget.check()  # never start a call the run limit has already reached
        argv = self._build_argv(prompt, role, read_only, session_name, structured=structured)
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
        progress: dict = {}
        try:
            if structured:
                returncode, reply, detail, usage = self._structured_call(
                    argv, role, session_name, progress
                )
            else:
                returncode, reply, detail, usage = self._plain_call(argv, role)
        except (Exception, KeyboardInterrupt):
            # a timeout or crash still consumed wall-clock time and credit. Whatever
            # copilot already reported is real and is kept, but it can only ever be a
            # lower bound: the invocation died before it could say what else it billed.
            partial = mark_incomplete(progress.get("usage"))
            known, _ = cost_report(partial)
            self.budget.settle(known, complete=False)
            self._record(role, role_cfg, session_name, started, ok=False, usage=partial)
            emit(
                self.config.events,
                "agent_call_finished",
                role=role,
                session=session_name,
                elapsed=round(time.monotonic() - started, 1),
                ok=False,
                usage=partial,
            )
            raise
        if returncode != 0:
            usage = mark_incomplete(usage)
        known, complete = cost_report(usage)
        self.budget.settle(known, complete=complete)
        elapsed = round(time.monotonic() - started, 1)
        reply = (reply or "").strip()
        ok = returncode == 0 and bool(reply)
        self._record(role, role_cfg, session_name, started, ok=ok, usage=usage, elapsed=elapsed)
        emit(
            self.config.events,
            "agent_call_finished",
            role=role,
            session=session_name,
            elapsed=elapsed,
            ok=ok,
            usage=usage,
        )
        if returncode != 0:
            raise CopilotError(
                f"copilot exited {returncode} for role {role}: {detail.strip()[:500]}"
            )
        return reply

    def _record(self, role, role_cfg, session_name, started, ok, usage, elapsed=None):
        self.usage.record(
            role=role,
            model=role_cfg.model,
            effort=role_cfg.effort,
            session=session_name,
            seconds=elapsed if elapsed is not None else round(time.monotonic() - started, 1),
            ok=ok,
            usage=usage,
        )

    # --- transports ----------------------------------------------------------

    def _plain_call(self, argv, role):
        """Test-only seam: an injected runner, `-s` text mode, no usage reported."""
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
        except OSError as e:
            raise CopilotError(f"could not start copilot for role {role}: {e}") from e
        return (
            result.returncode,
            result.stdout.strip(),
            result.stderr or result.stdout or "",
            None,
        )

    def _structured_call(self, argv, role, session_name, progress=None):
        """Run one invocation, reporting usage into `progress` as it arrives.

        `progress["usage"]` is updated on every model call, so a caller that
        sees this raise can still account the tokens and charges copilot had
        already reported — an interrupted call is not a free one.
        """
        progress = progress if progress is not None else {}
        try:
            proc = self.popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.config.repo_dir),
                # own a process group so a timeout can reap the whole tool tree by id
                start_new_session=True,
            )
        except OSError as e:
            raise CopilotError(f"could not start copilot for role {role}: {e}") from e
        lines: queue.Queue = queue.Queue()
        stderr_tail: deque = deque(maxlen=STDERR_TAIL_CHARS)
        reader_errors: list = []
        reaped = threading.Event()

        def _drain(stream, consume):
            try:
                for piece in stream:
                    consume(piece)
            except (OSError, ValueError) as e:
                # expected once we tear the pipes down ourselves; anything else is
                # a real transport fault and must not be swallowed
                if not reaped.is_set():
                    reader_errors.append(e)

        def _read_stdout():
            try:
                _drain(proc.stdout, lines.put)
            finally:
                lines.put(None)  # always unblock the consumer

        readers = [
            threading.Thread(target=_read_stdout, daemon=True),
            # stderr is drained concurrently: a chatty child must never block stdout
            threading.Thread(target=_drain, args=(proc.stderr, stderr_tail.extend), daemon=True),
        ]

        deadline = time.monotonic() + self.config.timeout
        final_message = ""
        failure_detail = ""
        usage = None
        saw_event = False
        exited = False
        raw: deque = deque(maxlen=RAW_STDOUT_LINES)
        try:
            for reader in readers:
                reader.start()
            while True:
                if time.monotonic() > deadline:
                    raise CopilotError(
                        f"copilot timed out after {self.config.timeout}s for role {role}"
                    )
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                raw.append(line)
                chunk, message, fail, call_usage, is_event = _parse_event_line(line, detail=True)
                saw_event = saw_event or is_event
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
                if call_usage is not None:
                    usage = merge_usage(usage, call_usage)
                    progress["usage"] = usage

            try:
                returncode = proc.wait(timeout=EXIT_GRACE_SECONDS)
            except (subprocess.TimeoutExpired, TimeoutError) as e:
                raise CopilotError(
                    f"copilot did not exit after closing stdout for role {role}"
                ) from e
            exited = True
            if reader_errors:
                raise CopilotError(
                    f"copilot output could not be read for role {role}: {reader_errors[0]}"
                ) from reader_errors[0]
        finally:
            # only force-kill when the child did not exit on its own; a clean exit
            # has nothing left to signal
            self._cleanup(proc, readers, reaped, force=not exited)

        if not saw_event and not final_message:
            # the command is not speaking copilot's event dialect (a shim, a wrapper,
            # an older CLI): fall back to raw stdout, with usage honestly unknown
            final_message = "".join(raw)
        detail = failure_detail or "".join(stderr_tail) or final_message
        return returncode, final_message.strip(), detail, usage

    def _cleanup(self, proc, readers, reaped, force: bool) -> None:
        """Bounded teardown: kill if needed, then join readers and close our pipes."""
        reaped.set()
        if force:
            self._terminate(proc)
        for reader in readers:
            reader.join(timeout=READER_JOIN_SECONDS)
        if any(reader.is_alive() for reader in readers):
            # A reaped leader can leave a child holding only stderr. Kill the
            # owned group before closing a pipe whose reader still holds its lock.
            if not force:
                self._terminate(proc)
            for reader in readers:
                reader.join(timeout=READER_JOIN_SECONDS)
            if any(reader.is_alive() for reader in readers):
                raise CopilotError("copilot output readers did not stop after process cleanup")
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError) as e:
                log.warning("could not close a copilot pipe: %s", e)

    def _terminate(self, proc) -> None:
        """Reap the process group we created — never by process name.

        `start_new_session=True` made the child its own group leader, so the
        group id *is* `proc.pid`. That is used directly rather than looking it
        up with `os.getpgid()`, which fails with ESRCH once the leader has
        exited — exactly the case where surviving grandchildren still need
        killing.
        """
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the whole group is already gone
            except OSError as e:
                log.warning("could not signal copilot process group %s: %s", pid, e)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("could not kill copilot process %s: %s", pid, e)
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, TimeoutError):
            log.warning("copilot process %s did not exit after SIGKILL", pid)

    # --- argv ----------------------------------------------------------------

    def _build_argv(self, prompt, role, read_only, session_name, structured=True):
        cfg = self.config
        argv = [cfg.copilot_cmd, "-p", prompt]
        argv += ["--output-format", "json"] if structured else ["-s"]
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


def cost_report(usage: dict | None) -> tuple[int | None, bool]:
    """(charge copilot reported in nano-AIU, whether that is the whole story).

    A partial figure is still real spend and is returned, but flagged: if
    copilot billed three model calls and costed one, banking that one as the
    complete figure would report the other two as free.
    """
    if not usage:
        return None, False
    return usage.get("nano_aiu"), bool(cost_is_complete(usage))


def measured_nano_aiu(usage: dict | None) -> int | None:
    """The invocation's charge when it is known in full, else None."""
    nano_aiu, complete = cost_report(usage)
    return nano_aiu if complete else None


def _extract_usage(data: dict) -> dict:
    """Pull tokens and the real credit charge out of a model_call_success event.

    Copilot reports OpenAI-style token names (`prompt_tokens`/`completion_tokens`);
    an earlier version of this parser looked for `input_tokens`/`output_tokens`
    and so silently recorded nothing. Anthropic-style names are still accepted in
    case the shape varies by provider.

    `copilotUsage.total_nano_aiu` is the actual credit charged for the call, in
    nano-AIU — the only trustworthy cost signal the CLI exposes.

    The event itself is the evidence a model call happened, so the result always
    carries `model_calls: 1`; an event with no usage payload is a call of
    unknown cost, which is not the same thing as a free call.
    """
    raw = data.get("responseUsage") or (data.get("responseChunk") or {}).get("usage") or {}
    usage: dict = {}
    incoming = raw.get("prompt_tokens", raw.get("input_tokens"))
    outgoing = raw.get("completion_tokens", raw.get("output_tokens"))
    if incoming is not None:
        usage["input_tokens"] = incoming
    if outgoing is not None:
        usage["output_tokens"] = outgoing
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        usage["cached_tokens"] = cached
    nano_aiu = (data.get("copilotUsage") or {}).get("total_nano_aiu")
    if nano_aiu is not None:
        usage["nano_aiu"] = nano_aiu
    usage["model_calls"] = 1
    usage["costed_model_calls"] = 1 if nano_aiu is not None else 0
    return usage


def _parse_event_line(line: str, detail: bool = False):
    """Map one copilot JSONL event to (output_chunk, message, failure, usage).

    With `detail=True` a fifth element says whether the line was a recognisable
    copilot event at all, which is how the caller tells "this binary does not
    speak JSONL" apart from "it does and said nothing".
    """
    chunk, message, failure, usage, is_event = _parse(line.strip())
    return (
        (chunk, message, failure, usage, is_event) if detail else (chunk, message, failure, usage)
    )


def _parse(line: str):
    if not line or not line.startswith("{"):
        return None, None, None, None, False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None, None, False
    if not isinstance(event, dict) or "type" not in event:
        return None, None, None, None, False
    kind = event.get("type", "")
    data = event.get("data") or {}

    if kind == "assistant.reasoning_delta":
        return data.get("deltaContent"), None, None, None, True
    if kind == "assistant.message_delta":
        return data.get("deltaContent"), None, None, None, True
    if kind == "tool.execution_start":
        name = data.get("toolName", "tool")
        args = json.dumps(data.get("arguments", {}))[:120]
        return f"\n⚙ {name} {args}\n", None, None, None, True
    if kind == "assistant.message":
        return None, data.get("content"), None, None, True
    if kind == "model.model_call_success":
        return None, None, None, _extract_usage(data), True
    if kind in ("model.turn_failed", "session.error"):
        return None, None, json.dumps(data)[:500], None, True
    return None, None, None, None, True
