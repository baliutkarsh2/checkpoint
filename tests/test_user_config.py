"""Tests for the user-level config (~/.checkpoint/config.json).

Uses the CHECKPOINT_HOME env var override so the tests don't touch the user's
real ~/.checkpoint directory.
"""
from __future__ import annotations

import pytest

from checkpoint.user_config import (
    KNOWN_KEYS,
    UserConfig,
    _coerce_value,
    config_path,
    home_dir,
)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    return tmp_path


def test_home_dir_uses_env_override(tmp_home):
    assert home_dir() == tmp_home


def test_config_path_under_home(tmp_home):
    assert config_path() == tmp_home / "config.json"


def test_load_returns_empty_when_missing(tmp_home):
    cfg = UserConfig.load()
    assert cfg.data == {}
    assert cfg.path == tmp_home / "config.json"


def test_set_get_roundtrip(tmp_home):
    cfg = UserConfig.load()
    cfg.set("defaults.judge_model", "gpt-4o")
    cfg.save()
    cfg2 = UserConfig.load()
    assert cfg2.get("defaults.judge_model") == "gpt-4o"


def test_set_creates_nested_keys(tmp_home):
    cfg = UserConfig.load()
    cfg.set("a.b.c", "deep")
    assert cfg.data == {"a": {"b": {"c": "deep"}}}


def test_unset_removes_key_and_prunes(tmp_home):
    cfg = UserConfig.load()
    cfg.set("a.b.c", "x")
    assert cfg.unset("a.b.c") is True
    # Branch should be pruned entirely.
    assert cfg.data == {}


def test_unset_missing_returns_false(tmp_home):
    cfg = UserConfig.load()
    assert cfg.unset("never.set") is False


def test_value_coercion_for_bool_int_float():
    assert _coerce_value("true") is True
    assert _coerce_value("False") is False
    assert _coerce_value("42") == 42
    assert _coerce_value("3.14") == 3.14
    assert _coerce_value("plain") == "plain"
    assert _coerce_value("env:OPENAI_API_KEY") == "env:OPENAI_API_KEY"


def test_env_indirection_resolved_on_get(tmp_home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hello")
    cfg = UserConfig.load()
    cfg.set("engine.openai_api_key", "env:MY_SECRET")
    assert cfg.get("engine.openai_api_key") == "hello"
    # When resolve_env=False, the literal env: ref comes back.
    assert cfg.get("engine.openai_api_key", resolve_env=False) == "env:MY_SECRET"


def test_env_indirection_returns_none_when_env_missing(tmp_home, monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
    cfg = UserConfig.load()
    cfg.set("engine.openai_api_key", "env:DEFINITELY_UNSET")
    assert cfg.get("engine.openai_api_key") is None


def test_flatten_returns_sorted_dotted_keys(tmp_home):
    cfg = UserConfig.load()
    cfg.set("z.b", 1)
    cfg.set("a.x", 2)
    cfg.set("a.y", 3)
    flat = cfg.flatten()
    assert list(flat.keys()) == ["a.x", "a.y", "z.b"]


def test_load_tolerates_corrupt_json(tmp_home):
    config_path().write_text("not json", encoding="utf-8")
    cfg = UserConfig.load()
    assert cfg.data == {}


def test_known_keys_documented():
    """Sanity check: every entry has a non-empty description."""
    for k, v in KNOWN_KEYS.items():
        assert v and len(v) > 5, f"{k} description too short"
