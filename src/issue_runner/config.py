"""Runner configuration.

Per-role model/effort keeps free-plan credit spend controllable: e.g. a cheap
model for the verifier, the default for the coder. A `runner.toml` in the
target repo (or passed via --config) provides defaults; CLI flags override.
BYOK (COPILOT_PROVIDER_* env vars) passes straight through the environment, so
pointing the whole runner at a local model needs no config here.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROLES = ("planner", "builder.tester", "builder.coder", "verifier")


@dataclass
class RoleConfig:
    model: str | None = None
    effort: str | None = None


@dataclass
class RunnerConfig:
    repo_dir: Path
    repo: str | None = None  # owner/name for gh; None = infer from repo_dir remote
    test_cmd: str = "pytest {test_path} -q"
    max_rounds: int = 3
    tester_retries: int = 2
    coder_retries: int = 2
    github_tickets: bool = True
    copilot_cmd: str = "copilot"
    max_ai_credits: int | None = None
    timeout: int = 1800
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    tickets_backend: object | None = None  # set by the CLI, never from runner.toml
    retry_blocked: bool = False

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name, RoleConfig())


def load_config(repo_dir: Path, config_path: Path | None = None) -> RunnerConfig:
    cfg = RunnerConfig(repo_dir=repo_dir)
    path = config_path or repo_dir / "runner.toml"
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text())
    for key in (
        "test_cmd",
        "max_rounds",
        "tester_retries",
        "coder_retries",
        "github_tickets",
        "copilot_cmd",
        "max_ai_credits",
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
