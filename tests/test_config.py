"""Phase 4 plan 04 — .checkpoint.json + harness.json autoload + precedence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkpoint.config import (
    CheckpointConfig,
    HarnessConfig,
    EvaluatorResolution,
    find_upward,
    load_checkpoint_config,
    load_harness_config,
    matches_tag,
    resolve_evaluator_model,
)


# ---- find_upward -----------------------------------------------------------

def test_find_upward_same_dir(tmp_path: Path):
    (tmp_path / ".checkpoint.json").write_text("{}")
    assert find_upward(tmp_path, ".checkpoint.json") == (tmp_path / ".checkpoint.json").resolve()


def test_find_upward_parent_dir(tmp_path: Path):
    (tmp_path / ".checkpoint.json").write_text("{}")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert find_upward(child, ".checkpoint.json") == (tmp_path / ".checkpoint.json").resolve()


def test_find_upward_not_found(tmp_path: Path):
    assert find_upward(tmp_path, ".checkpoint.json") is None


def test_find_upward_respects_max_levels(tmp_path: Path):
    (tmp_path / ".checkpoint.json").write_text("{}")
    deep = tmp_path
    for i in range(7):
        deep = deep / f"l{i}"
    deep.mkdir(parents=True)
    # 7 levels deep > max_levels=5 → not found.
    assert find_upward(deep, ".checkpoint.json", max_levels=5) is None


# ---- load_checkpoint_config ------------------------------------------------

def test_load_checkpoint_config_empty_when_missing(tmp_path: Path):
    cfg = load_checkpoint_config(tmp_path)
    assert cfg.clones == []
    assert cfg.evaluator_model is None
    assert cfg.source_path is None


def test_load_checkpoint_config_full(tmp_path: Path):
    (tmp_path / ".checkpoint.json").write_text(json.dumps({
        "clones": ["github", "slack"],
        "harness": {"path": "./harness.py", "promptFiles": ["./prompts/*.md"]},
        "evaluator": {"model": "gpt-5-mini"},
        "seeds": {"github": "small-project", "slack": "engineering-team"},
    }))
    cfg = load_checkpoint_config(tmp_path)
    assert cfg.clones == ["github", "slack"]
    assert cfg.harness_path == "./harness.py"
    assert cfg.prompt_files == ["./prompts/*.md"]
    assert cfg.evaluator_model == "gpt-5-mini"
    assert cfg.seeds == {"github": "small-project", "slack": "engineering-team"}
    assert cfg.source_path is not None


def test_load_checkpoint_config_malformed_silently_empty(tmp_path: Path):
    (tmp_path / ".checkpoint.json").write_text("{not json")
    cfg = load_checkpoint_config(tmp_path)
    assert cfg.clones == []


# ---- load_harness_config ---------------------------------------------------

def test_load_harness_config_from_dir(tmp_path: Path):
    (tmp_path / "harness.json").write_text(json.dumps({
        "path": "harness.py", "env": {"FOO": "bar"}, "dockerfile": "./Dockerfile",
    }))
    hc = load_harness_config(str(tmp_path))
    assert hc.path == "harness.py"
    assert hc.env == {"FOO": "bar"}
    assert hc.dockerfile == "./Dockerfile"


def test_load_harness_config_from_file_path(tmp_path: Path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"path": "h.py"}))
    hc = load_harness_config(str(p))
    assert hc.path == "h.py"


def test_load_harness_config_walks_up_when_none(tmp_path: Path, monkeypatch):
    (tmp_path / "harness.json").write_text(json.dumps({"path": "h.py"}))
    child = tmp_path / "a"
    child.mkdir()
    hc = load_harness_config(None, cwd_start=child)
    assert hc.path == "h.py"


def test_load_harness_config_returns_empty_for_command(tmp_path: Path):
    # A shell command isn't a harness.json — should not load anything.
    hc = load_harness_config("python harness.py")
    assert hc.path is None


# ---- resolve_evaluator_model -----------------------------------------------

def test_resolve_evaluator_flag_wins():
    r = resolve_evaluator_model("flag-m", "scenario-m", "config-m", "env-m")
    assert r.model == "flag-m" and r.source == "flag"


def test_resolve_evaluator_scenario_beats_config():
    r = resolve_evaluator_model(None, "scenario-m", "config-m", "env-m")
    assert r.model == "scenario-m" and r.source == "scenario"


def test_resolve_evaluator_config_beats_env():
    r = resolve_evaluator_model(None, None, "config-m", "env-m")
    assert r.model == "config-m" and r.source == "config"


def test_resolve_evaluator_env_beats_default():
    r = resolve_evaluator_model(None, None, None, "env-m")
    assert r.model == "env-m" and r.source == "env"


def test_resolve_evaluator_default():
    r = resolve_evaluator_model(None, None, None, None)
    assert r.model == "gpt-4o-mini" and r.source == "default"


def test_resolve_evaluator_empty_string_treated_as_none():
    r = resolve_evaluator_model("", "scenario-m", None, None)
    assert r.source == "scenario"


# ---- matches_tag (SCN-10) --------------------------------------------------

def test_matches_tag_no_filter_always_runs():
    assert matches_tag(None, None) is True
    assert matches_tag("smoke", None) is True


def test_matches_tag_scenario_without_tags_filtered_out():
    assert matches_tag(None, "smoke") is False
    assert matches_tag("", "smoke") is False


def test_matches_tag_match():
    assert matches_tag("smoke", "smoke") is True
    assert matches_tag("smoke, slow, regression", "regression") is True


def test_matches_tag_case_insensitive():
    assert matches_tag("Smoke", "smoke") is True


def test_matches_tag_no_partial():
    assert matches_tag("smoke-1", "smoke") is False
