"""Subprocess wrapper around the GitHub Copilot CLI (non-interactive mode).

Every model call goes through CopilotClient.run(). The `runner` callable is
injectable so tests never spawn a real process, and `copilot_cmd` is
configurable so end-to-end tests can substitute a fake binary or a BYOK-backed
invocation.

`git push` is denied unconditionally: the runner commits on a branch, pushing
is a human decision. Deny rules take precedence over --allow-all-tools.
"""

import subprocess
from pathlib import Path

from .config import RunnerConfig

ALWAYS_DENY = ("shell(git push)",)
READ_ONLY_DENY = ("write", "shell(git:*)")


class CopilotError(RuntimeError):
    pass


class CopilotClient:
    def __init__(self, config: RunnerConfig, runner=subprocess.run):
        self.config = config
        self.runner = runner

    def run(
        self,
        prompt: str,
        role: str,
        read_only: bool = False,
        session_name: str | None = None,
    ) -> str:
        argv = self._build_argv(prompt, role, read_only, session_name)
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
        if result.returncode != 0:
            raise CopilotError(
                f"copilot exited {result.returncode} for role {role}: "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
        return result.stdout.strip()

    def _build_argv(
        self, prompt: str, role: str, read_only: bool, session_name: str | None
    ) -> list[str]:
        cfg = self.config
        argv = [
            cfg.copilot_cmd,
            "-p",
            prompt,
            "-s",
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
