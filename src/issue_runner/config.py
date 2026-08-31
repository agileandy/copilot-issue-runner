"""Runner configuration.

Per-role model/effort keeps free-plan credit spend controllable: e.g. a cheap
model for the verifier, the default for the coder. A `runner.toml` in the
target repo (or passed via --config) provides defaults; CLI flags override.
BYOK (COPILOT_PROVIDER_* env vars) passes straight through the environment, so
pointing the whole runner at a local model needs no config here.
"""

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .testcmd import DEFAULT_TEST_CMD, detect_test_cmd

ROLES = ("planner", "builder.tester", "builder.coder", "verifier")

log = logging.getLogger("issue_runner")


@dataclass
class RoleConfig:
    model: str | None = None
    effort: str | None = None


@dataclass
class RunnerConfig:
    repo_dir: Path
    repo: str | None = None  # owner/name for gh; None = infer from repo_dir remote
    test_cmd: str = DEFAULT_TEST_CMD
    max_rounds: int = 3
    tester_retries: int = 2
    coder_retries: int = 2
    github_tickets: bool = True
    open_pr: bool = True
    copilot_cmd: str = "copilot"
    visual: bool = False
    max_ai_credits: int | None = None
    max_run_credits: int | None = None
    empty_reply_retries: int = 2
    timeout: int = 1800
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    tickets_backend: object | None = None  # set by the CLI, never from runner.toml
    events: object | None = None  # EventBus, set by the CLI; never from runner.toml
    retry_blocked: bool = False

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name, RoleConfig())


def _apply_detected_test_cmd(cfg: RunnerConfig) -> None:
    """Fill in test_cmd only when the user has not specified one."""
    found = detect_test_cmd(cfg.repo_dir)
    cfg.test_cmd = found.test_cmd
    if found.marker:
        log.info("detected %s — test_cmd: %s", found.marker, found.test_cmd)
    else:
        log.warning(
            "no project marker found in %s — falling back to test_cmd: %s "
            "(set test_cmd in runner.toml or pass --test-cmd)",
            cfg.repo_dir,
            found.test_cmd,
        )


def load_config(repo_dir: Path, config_path: Path | None = None) -> RunnerConfig:
    cfg = RunnerConfig(repo_dir=repo_dir)
    path = config_path or repo_dir / "runner.toml"
    if not path.exists():
        _apply_detected_test_cmd(cfg)
        return cfg
    data = tomllib.loads(path.read_text())
    if "test_cmd" not in data:
        _apply_detected_test_cmd(cfg)
    for key in (
        "test_cmd",
        "max_rounds",
        "tester_retries",
        "coder_retries",
        "github_tickets",
        "open_pr",
        "copilot_cmd",
        "visual",
        "max_ai_credits",
        "max_run_credits",
        "empty_reply_retries",
        "timeout",
        "repo",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    for role_name, role_data in data.get("roles", {}).items():
        cfg.roles[role_name] = RoleConfig(
            model=role_data.get("model"), effort=role_data.get("effort")
        )
    return cfg
