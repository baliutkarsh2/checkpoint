"""``checkpoint whoami`` — local-only identity / environment summary.

Checkpoint is a *local* tool: there is no remote workspace, no login flow,
no account.  ``whoami`` instead reports everything a developer or oncaller
would need to answer "what state is my Checkpoint install in right now?":

  * the version of the package
  * where the user / project / runs / scenarios live on disk
  * which judge model will be used by default
  * whether the OpenAI key is reachable (without printing it)
  * how many cached runs / live clones / configured scenarios are present

Designed to be both pretty (Rich table when run interactively) and
machine-readable (``--json`` flag from cli.py).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .clone_manager import DEFAULT_REGISTRY
from .config import load_checkpoint_config
from .user_config import UserConfig, config_path, home_dir
from .run_record import RUNS_DIR


@dataclass
class Identity:
    version: str
    home: str
    user_config: str | None
    project_config: str | None
    scenarios_dir: str
    runs_dir: str
    runs_count: int
    live_clones: int
    judge_model: str
    judge_model_source: str  # "user-config" | "env" | "default"
    openai_key_present: bool
    python: str
    platform: str


def collect(scenarios_dir: Path | None = None) -> Identity:
    user_cfg = UserConfig.load()
    project_cfg = load_checkpoint_config()
    judge_model, judge_source = _resolve_judge_model(user_cfg)
    scenarios = scenarios_dir or _scenarios_default()
    return Identity(
        version=__version__,
        home=str(home_dir()),
        user_config=str(config_path()) if config_path().exists() else None,
        project_config=project_cfg.source_path,
        scenarios_dir=str(scenarios),
        runs_dir=str(RUNS_DIR.resolve()),
        runs_count=_count_runs(),
        live_clones=_count_live_clones(),
        judge_model=judge_model,
        judge_model_source=judge_source,
        openai_key_present=bool(os.environ.get("OPENAI_API_KEY")),
        python=_python_version(),
        platform=_platform(),
    )


def to_dict(ident: Identity) -> dict:
    return asdict(ident)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _resolve_judge_model(cfg: UserConfig) -> tuple[str, str]:
    v = cfg.get("defaults.judge_model")
    if v:
        return str(v), "user-config"
    env = os.environ.get("CHECKPOINT_JUDGE_MODEL")
    if env:
        return env, "env"
    return "gpt-4o-mini", "default"


def _scenarios_default() -> Path:
    cwd_scenarios = Path.cwd() / "scenarios"
    if cwd_scenarios.is_dir():
        return cwd_scenarios.resolve()
    return Path.cwd().resolve()


def _count_runs() -> int:
    if not RUNS_DIR.exists():
        return 0
    return sum(1 for _ in RUNS_DIR.glob("*.json"))


def _count_live_clones() -> int:
    if not DEFAULT_REGISTRY.exists():
        return 0
    try:
        data = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return len(data) if isinstance(data, dict) else 0


def _python_version() -> str:
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _platform() -> str:
    import platform
    return f"{platform.system()} {platform.release()} ({platform.machine()})"
