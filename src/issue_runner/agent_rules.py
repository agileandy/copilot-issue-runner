"""Rules every agent prompt carries about its workspace and toolchain.

Agents run non-interactively, so a denied tool call cannot be approved and is
simply lost. Most denials came from agents leaving the run worktree (the source
checkout's `.venv`, `cd` into another checkout) or retrying the same denied
command. The brief therefore names the exact test commands for this worktree
and says what to do when a call is denied.
"""

import shlex
from pathlib import Path

from .config import RunnerConfig
from .testcmd import detect_regression_cmd


def test_commands(cfg: RunnerConfig, test_path: str | None = None) -> tuple[str, str | None]:
    """(focused test command, full-suite command) as the agent should type them."""
    selector = shlex.quote(test_path) if test_path else "<test_path>"
    focused = cfg.test_cmd.format(test_path=selector)
    regression = cfg.regression_cmd or detect_regression_cmd(Path(cfg.repo_dir))
    return focused, regression


def workspace_rules(cfg: RunnerConfig, test_path: str | None = None) -> str:
    focused, regression = test_commands(cfg, test_path)
    lines = [
        "WORKSPACE RULES:",
        (
            f"- Your workspace is {Path(cfg.repo_dir)}. Run every command from it and never "
            "cd to, read from or run tools in any other checkout."
        ),
        f"- Run the test for this sub-task with: `{focused}`",
    ]
    if regression:
        lines.append(f"- Run the full suite with: `{regression}`")
    lines += [
        (
            "- Never call the primary checkout's .venv or node_modules. This workspace has "
            "its own toolchain."
        ),
        "- Use uv, never pip.",
        (
            "- If a tool call is denied, try one different approach, then report the denied "
            "command in your reply; never repeat it."
        ),
    ]
    return "\n".join(lines) + "\n"
