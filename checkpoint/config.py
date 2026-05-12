"""`.checkpoint.json` and `harness.json` autoload + evaluator-model precedence.

`.checkpoint.json` schema (Archal-compatible):
{
  "clones": ["github", "slack"],          # default clones if scenario omits
  "harness": {
    "path": "./example/harness.py",       # default harness command (file)
    "promptFiles": ["./prompts/**.md"]    # globs (unused by core; for skills)
  },
  "evaluator": {"model": "gpt-4o-mini"},
  "seeds": {"github": "small-project"}    # default named seeds
}

`harness.json` schema (sits next to a harness):
{
  "path": "harness.py",
  "promptFiles": ["./prompts/*.md"],
  "env": {...},
  "dockerfile": "./Dockerfile"
}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


CHECKPOINT_CONFIG = ".checkpoint.json"
HARNESS_CONFIG = "harness.json"


@dataclass
class CheckpointConfig:
    clones: list[str] = field(default_factory=list)
    harness_path: str | None = None
    prompt_files: list[str] = field(default_factory=list)
    evaluator_model: str | None = None
    seeds: dict[str, str] = field(default_factory=dict)
    source_path: str | None = None  # absolute path the config was loaded from


@dataclass
class HarnessConfig:
    path: str | None = None
    prompt_files: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    dockerfile: str | None = None
    source_path: str | None = None


def find_upward(start: Path | str, filename: str, max_levels: int = 5) -> Path | None:
    """Walk up from `start` looking for `filename`. Returns absolute path or None."""
    p = Path(start).resolve()
    for _ in range(max_levels + 1):
        candidate = p / filename
        if candidate.is_file():
            return candidate
        if p.parent == p:
            return None
        p = p.parent
    return None


def load_checkpoint_config(start: Path | str | None = None) -> CheckpointConfig:
    """Walk up from start (default cwd) to find a `.checkpoint.json`.

    Returns an empty `CheckpointConfig` if none found. Malformed JSON is
    treated as no-config (logged once via stderr, not raised).
    """
    start = start or Path.cwd()
    p = find_upward(start, CHECKPOINT_CONFIG)
    if p is None:
        return CheckpointConfig()
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckpointConfig()
    cfg = CheckpointConfig(source_path=str(p))
    if isinstance(raw.get("clones"), list):
        cfg.clones = [str(c) for c in raw["clones"]]
    harness = raw.get("harness") or {}
    if isinstance(harness, dict):
        if isinstance(harness.get("path"), str):
            cfg.harness_path = harness["path"]
        if isinstance(harness.get("promptFiles"), list):
            cfg.prompt_files = [str(x) for x in harness["promptFiles"]]
    evaluator = raw.get("evaluator") or {}
    if isinstance(evaluator, dict) and isinstance(evaluator.get("model"), str):
        cfg.evaluator_model = evaluator["model"]
    seeds = raw.get("seeds") or {}
    if isinstance(seeds, dict):
        cfg.seeds = {str(k): str(v) for k, v in seeds.items()}
    return cfg


def load_harness_config(harness_arg: str | None, cwd_start: Path | str | None = None) -> HarnessConfig:
    """Load a `harness.json` based on what the user passed to --harness.

    Resolution rules:
      - If `harness_arg` is None: look for `harness.json` in cwd (walk up).
      - If `harness_arg` is a directory: look for `harness.json` inside it.
      - If `harness_arg` is a file ending in `harness.json`: load it directly.
      - Otherwise (a command string or a path to a script): no harness.json
        lookup — return empty config.
    """
    p: Path | None = None
    if harness_arg is None:
        p = find_upward(cwd_start or Path.cwd(), HARNESS_CONFIG)
    else:
        arg_path = Path(harness_arg)
        if arg_path.is_dir():
            candidate = arg_path / HARNESS_CONFIG
            if candidate.is_file():
                p = candidate
        elif arg_path.is_file() and arg_path.name == HARNESS_CONFIG:
            p = arg_path
    if p is None:
        return HarnessConfig()
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return HarnessConfig()
    hc = HarnessConfig(source_path=str(p))
    if isinstance(raw.get("path"), str):
        hc.path = raw["path"]
    if isinstance(raw.get("promptFiles"), list):
        hc.prompt_files = [str(x) for x in raw["promptFiles"]]
    env = raw.get("env") or {}
    if isinstance(env, dict):
        hc.env = {str(k): str(v) for k, v in env.items()}
    if isinstance(raw.get("dockerfile"), str):
        hc.dockerfile = raw["dockerfile"]
    return hc


@dataclass
class EvaluatorResolution:
    model: str
    source: str  # "flag" | "scenario" | "config" | "env" | "default"


def resolve_evaluator_model(
    flag_value: str | None,
    scenario_value: str | None,
    config_value: str | None,
    env_value: str | None,
    default: str = "gpt-4o-mini",
) -> EvaluatorResolution:
    """Precedence: flag > scenario > config > env > default.

    A value of None/"" is treated as "not specified". The CLI default for
    --model is `None` in Phase 4 (was `gpt-4o-mini` in v0) so the precedence
    chain can actually see whether the user passed it.
    """
    if flag_value:
        return EvaluatorResolution(flag_value, "flag")
    if scenario_value:
        return EvaluatorResolution(scenario_value, "scenario")
    if config_value:
        return EvaluatorResolution(config_value, "config")
    if env_value:
        return EvaluatorResolution(env_value, "env")
    return EvaluatorResolution(default, "default")


def matches_tag(scenario_tags_raw: str | None, filter_tag: str | None) -> bool:
    """SCN-10: return True if the scenario should run under the given --tag filter.

    No filter -> always run. Scenario without a `tags:` config -> filtered
    out (only tagged scenarios match a tag filter).
    """
    if not filter_tag:
        return True
    if not scenario_tags_raw:
        return False
    tags = {t.strip().lower() for t in str(scenario_tags_raw).split(",") if t.strip()}
    return filter_tag.strip().lower() in tags
