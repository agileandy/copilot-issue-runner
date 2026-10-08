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

from .control import RunControl
from .testcmd import DEFAULT_TEST_CMD, detect_test_cmd

ROLES = (
    "planner",
    "builder.tester",
    "builder.coder",
    "verifier",
    "acceptor",
    "acceptance.tester",
    "reviser",
    "resolver",
)

log = logging.getLogger("issue_runner")


class ConfigError(ValueError):
    pass


@dataclass
class RoleConfig:
    model: str | None = None
    effort: str | None = None


MERGE_METHODS = ("auto", "squash", "merge", "rebase")


@dataclass
class DeployConfig:
    """The `[deploy]` table: how a --deploy run reviews, merges and watches Dev."""

    review_bot: str = "copilot-pull-request-reviewer"
    pr_title: str = "feat: {title}"  # under squash merge this becomes the commit subject
    review_timeout_min: int = 30
    max_review_rounds: int = 3
    merge_method: str = "auto"
    max_merge_attempts: int = 3
    workflow: str = "dev-deployment.yaml"
    environment: str = "development"
    deploy_start_grace_min: int = 10
    deploy_timeout_min: int = 60
    dispatch_if_not_triggered: bool = False
    dev_url: str = ""
    dev_check_cmd: str = ""  # runs one Dev check: {check_path}, {dev_url}, {criterion}
    dev_check_ext: str = ".sh"  # file extension of a written check
    dev_check_guide: str = ""  # how checks are written for this repository
    dev_check_timeout_sec: int = 300
    poll_seconds: int = 30
    max_acceptance_rounds: int = 1


@dataclass
class PrePrConfig:
    """The `[pre_pr]` table: checks run on the branch before the PR opens."""

    commands: list[str] = field(default_factory=list)  # {base}: the commit the run branched from
    max_rounds: int = 2


_DEPLOY_INTS = (
    "review_timeout_min",
    "max_review_rounds",
    "max_merge_attempts",
    "deploy_start_grace_min",
    "deploy_timeout_min",
    "poll_seconds",
    "dev_check_timeout_sec",
)
_DEPLOY_STRS = (
    "review_bot",
    "pr_title",
    "workflow",
    "environment",
    "dev_url",
    "dev_check_cmd",
    "dev_check_ext",
    "dev_check_guide",
)


@dataclass
class RunnerConfig:
    repo_dir: Path
    repo: str | None = None  # owner/name for gh; None = infer from repo_dir remote
    test_cmd: str = DEFAULT_TEST_CMD
    test_cmd_detected: bool = False  # re-detected in the run worktree when True
    regression_cmd: str | None = None
    ticket_regression: str = "shared"  # per-ticket full suite: shared, focused or full
    # commands that prepare a run worktree instead of the discovered toolchain steps
    setup_cmd: list[str] | None = None
    isolate_worktree: bool = True
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
    provision: bool = True  # give each run worktree its own .venv / node_modules
    provision_timeout: int = 900  # seconds per provisioning step
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    tickets_backend: object | None = None  # set by the CLI, never from runner.toml
    events: object | None = None  # EventBus, set by the CLI; never from runner.toml
    retry_blocked: bool = False
    # the [deploy] settings; only used when `deploy` is set by --deploy
    deploy_settings: DeployConfig = field(default_factory=DeployConfig)
    deploy: bool = False
    pre_pr: PrePrConfig = field(default_factory=PrePrConfig)
    preflight: object | None = None  # set by the CLI for --deploy; never from runner.toml
    control: RunControl = field(default_factory=RunControl, repr=False)

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name, RoleConfig())


def apply_detected_test_cmd(cfg: RunnerConfig) -> None:
    """Fill in test_cmd only when the user has not specified one."""
    found = detect_test_cmd(cfg.repo_dir)
    cfg.test_cmd = found.test_cmd
    cfg.test_cmd_detected = True
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
        if config_path is not None:
            raise ConfigError(f"configuration file does not exist: {path}")
        apply_detected_test_cmd(cfg)
        return cfg
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot load configuration {path}: {e}") from e
    if "test_cmd" not in data:
        apply_detected_test_cmd(cfg)
    for key in (
        "test_cmd",
        "regression_cmd",
        "ticket_regression",
        "isolate_worktree",
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
        "provision",
        "provision_timeout",
        "repo",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    if "setup_cmd" in data:
        value = data["setup_cmd"]
        cfg.setup_cmd = [value] if isinstance(value, str) else value
    for role_name, role_data in data.get("roles", {}).items():
        cfg.roles[role_name] = RoleConfig(
            model=role_data.get("model"), effort=role_data.get("effort")
        )
    if "deploy" in data:
        cfg.deploy_settings = _load_deploy(data["deploy"])
    if "pre_pr" in data:
        cfg.pre_pr = _load_pre_pr(data["pre_pr"])
    return cfg


def _load_deploy(table) -> DeployConfig:
    if not isinstance(table, dict):
        raise ConfigError("[deploy] must be a table")
    known = set(DeployConfig.__dataclass_fields__)
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"unknown [deploy] setting(s): {', '.join(unknown)}")
    return DeployConfig(**table)


def _load_pre_pr(table) -> PrePrConfig:
    if not isinstance(table, dict):
        raise ConfigError("[pre_pr] must be a table")
    known = set(PrePrConfig.__dataclass_fields__)
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"unknown [pre_pr] setting(s): {', '.join(unknown)}")
    values = dict(table)
    if isinstance(values.get("commands"), str):
        values["commands"] = [values["commands"]]
    return PrePrConfig(**values)


def validate_config(cfg: RunnerConfig) -> None:
    for name in ("max_rounds", "tester_retries", "coder_retries", "empty_reply_retries"):
        value = getattr(cfg, name)
        if type(value) is not int or value < 0:
            raise ConfigError(f"{name} must be a non-negative integer")
    for name in ("timeout", "provision_timeout", "max_ai_credits", "max_run_credits"):
        value = getattr(cfg, name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ConfigError(f"{name} must be a positive integer")
    if not isinstance(cfg.test_cmd, str) or not cfg.test_cmd.strip():
        raise ConfigError("test_cmd must be a non-empty command")
    if cfg.regression_cmd is not None and (
        not isinstance(cfg.regression_cmd, str) or not cfg.regression_cmd.strip()
    ):
        raise ConfigError("regression_cmd must be a non-empty command")
    if cfg.ticket_regression not in ("shared", "focused", "full"):
        raise ConfigError("ticket_regression must be one of: shared, focused, full")
    if cfg.setup_cmd is not None and (
        not isinstance(cfg.setup_cmd, list)
        or not cfg.setup_cmd
        or not all(isinstance(c, str) and c.strip() for c in cfg.setup_cmd)
    ):
        raise ConfigError("setup_cmd must be a command or a list of non-empty commands")
    for name in ("isolate_worktree", "provision"):
        if type(getattr(cfg, name)) is not bool:
            raise ConfigError(f"{name} must be true or false")
    _validate_deploy(cfg.deploy_settings)
    _validate_pre_pr(cfg.pre_pr)


def _validate_deploy(d: DeployConfig) -> None:
    for name in _DEPLOY_INTS:
        value = getattr(d, name)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"[deploy] {name} must be a positive integer")
    if type(d.max_acceptance_rounds) is not int or d.max_acceptance_rounds < 0:
        raise ConfigError("[deploy] max_acceptance_rounds must be a non-negative integer")
    for name in _DEPLOY_STRS:
        if not isinstance(getattr(d, name), str):
            raise ConfigError(f"[deploy] {name} must be a string")
    for name in ("review_bot", "workflow", "environment"):
        if not getattr(d, name).strip():
            raise ConfigError(f"[deploy] {name} must not be empty")
    if not d.dev_check_ext.startswith(".") or "/" in d.dev_check_ext:
        raise ConfigError("[deploy] dev_check_ext must be a file extension such as '.sh'")
    try:
        d.pr_title.format(title="t", number=1)
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"[deploy] pr_title may use only {{title}} and {{number}}: {e}") from e
    if d.merge_method not in MERGE_METHODS:
        raise ConfigError(f"[deploy] merge_method must be one of {', '.join(MERGE_METHODS)}")
    if type(d.dispatch_if_not_triggered) is not bool:
        raise ConfigError("[deploy] dispatch_if_not_triggered must be true or false")


def _validate_pre_pr(p: PrePrConfig) -> None:
    if type(p.max_rounds) is not int or p.max_rounds < 0:
        raise ConfigError("[pre_pr] max_rounds must be a non-negative integer")
    if not isinstance(p.commands, list) or not all(
        isinstance(c, str) and c.strip() for c in p.commands
    ):
        raise ConfigError("[pre_pr] commands must be a list of non-empty commands")
