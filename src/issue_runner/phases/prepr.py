"""Pre-PR phase: run the configured check commands on the branch and collect findings."""

import shlex
import subprocess

from ..config import RunnerConfig
from ..testreport import strip_ansi

MAX_FINDING_CHARS = 3000


def run_checks(cfg: RunnerConfig, base: str) -> list[dict]:
    """Run each `[pre_pr]` command; return one finding per failing command, in order."""
    findings: list[dict] = []
    for cmd in cfg.pre_pr.commands:
        cfg.control.check()
        cmd = cmd.replace("{base}", base)
        try:
            result = subprocess.run(
                shlex.split(cmd),
                cwd=cfg.repo_dir,
                capture_output=True,
                text=True,
                timeout=cfg.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            findings.append({"command": cmd, "text": str(e)})
            continue
        if result.returncode == 0:
            continue
        text = strip_ansi((result.stdout or "") + (result.stderr or "")).strip()
        findings.append({"command": cmd, "text": text[-MAX_FINDING_CHARS:]})
    return findings
