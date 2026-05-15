"""Tests for the v0.2 CLI surface additions (Sprint A).

Covers:
  - whoami (table + --json)
  - config init / show / set / get / unset / path
  - debug usage / export / inspect
  - clone list / status (alias for inspect)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.user_config import UserConfig


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect ~/.checkpoint and the runs dir into a tmp_path so tests don't
    touch the developer's machine state."""
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------

def test_whoami_table(isolated_home):
    r = CliRunner().invoke(main, ["whoami"])
    assert r.exit_code == 0
    assert "Version" in r.output
    assert "Judge model" in r.output


def test_whoami_json(isolated_home):
    r = CliRunner().invoke(main, ["whoami", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert "version" in payload
    assert "judge_model" in payload
    assert payload["judge_model_source"] in ("user-config", "env", "default")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_config_path_prints_path(isolated_home):
    r = CliRunner().invoke(main, ["config", "path"])
    assert r.exit_code == 0
    assert "config.json" in r.output


def test_config_show_when_unset(isolated_home):
    r = CliRunner().invoke(main, ["config", "show"])
    assert r.exit_code == 0
    assert "No user config" in r.output or "config init" in r.output


def test_config_init_creates_file(isolated_home):
    r = CliRunner().invoke(main, ["config", "init"])
    assert r.exit_code == 0
    assert (isolated_home / "config.json").exists()


def test_config_init_refuses_overwrite_without_force(isolated_home):
    CliRunner().invoke(main, ["config", "init"])
    r = CliRunner().invoke(main, ["config", "init"])
    assert r.exit_code == 1
    assert "Use --force" in r.output


def test_config_init_with_force_overwrites(isolated_home):
    CliRunner().invoke(main, ["config", "init"])
    r = CliRunner().invoke(main, ["config", "init", "--force"])
    assert r.exit_code == 0


def test_config_set_get_unset_roundtrip(isolated_home):
    CliRunner().invoke(main, ["config", "init"])
    r = CliRunner().invoke(main, ["config", "set", "defaults.judge_model", "gpt-5"])
    assert r.exit_code == 0
    r = CliRunner().invoke(main, ["config", "get", "defaults.judge_model"])
    assert r.exit_code == 0
    assert r.output.strip() == "gpt-5"
    r = CliRunner().invoke(main, ["config", "unset", "defaults.judge_model"])
    assert r.exit_code == 0
    r = CliRunner().invoke(main, ["config", "get", "defaults.judge_model"])
    assert r.exit_code == 1


def test_config_set_warns_on_unknown_key(isolated_home):
    r = CliRunner().invoke(main, ["config", "set", "totally.unknown.key", "x"])
    assert r.exit_code == 0
    assert "not a known config key" in r.output


def test_config_show_json(isolated_home):
    CliRunner().invoke(main, ["config", "init"])
    r = CliRunner().invoke(main, ["config", "show", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert "defaults.judge_model" in payload


def test_config_set_env_indirection_resolved(isolated_home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2")
    CliRunner().invoke(main, ["config", "set", "engine.openai_api_key", "env:MY_SECRET"])
    cfg = UserConfig.load()
    # Default get resolves env; reveal_env=False shows the literal.
    assert cfg.get("engine.openai_api_key") == "hunter2"
    assert cfg.get("engine.openai_api_key", resolve_env=False) == "env:MY_SECRET"


# ---------------------------------------------------------------------------
# debug usage / export / inspect
# ---------------------------------------------------------------------------

def test_debug_usage_with_no_runs(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["debug", "usage"])
    assert r.exit_code == 0
    assert "Total runs" in r.output


def test_debug_usage_json(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["debug", "usage", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["total_runs"] == 0


def test_debug_export_anonymizes_pii(isolated_home, tmp_path, monkeypatch):
    """A trace containing emails and tokens comes back redacted."""
    runs_dir = tmp_path / ".checkpoint" / "cache" / "runs"
    runs_dir.mkdir(parents=True)
    rec = {
        "run_id": "abc123abc123",
        "scenario": "x",
        "satisfaction": 100,
        "criteria": [],
        "trace": [{"path": "/users", "body": {"email": "alice@example.com"}}],
        "state": {
            "auth_header": "ghp_AaBbCcDdEeFfGgHhIiJj1234567890",
            "openai_key": "sk-AbCdEf1234567890abcdef",
        },
        "env": {"timestamp": "2026-05-14T00:00:00Z"},
    }
    (runs_dir / "abc123abc123.json").write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "exported.json"
    r = CliRunner().invoke(main, ["debug", "export", "abc123abc123", "-o", str(out), "--anonymize"])
    assert r.exit_code == 0, r.output
    blob = out.read_text(encoding="utf-8")
    assert "alice@example.com" not in blob
    assert "user@example.com" in blob
    assert "ghp_AaBbCc" not in blob
    assert "ghp_REDACTED" in blob
    assert "sk-AbCdEf" not in blob
    assert "sk-REDACTED" in blob


def test_debug_export_without_anonymize_keeps_data(isolated_home, tmp_path, monkeypatch):
    runs_dir = tmp_path / ".checkpoint" / "cache" / "runs"
    runs_dir.mkdir(parents=True)
    rec = {"run_id": "xyz789xyz789", "scenario": "x", "satisfaction": 100,
           "criteria": [], "trace": [], "state": {"email": "bob@example.com"},
           "env": {"timestamp": "2026-05-14T00:00:00Z"}}
    (runs_dir / "xyz789xyz789.json").write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "exported.json"
    r = CliRunner().invoke(main, ["debug", "export", "xyz789xyz789", "-o", str(out)])
    assert r.exit_code == 0, r.output
    blob = out.read_text(encoding="utf-8")
    assert "bob@example.com" in blob


def test_debug_inspect_unknown_run(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["debug", "inspect", "nope"])
    assert r.exit_code == 1


# ---------------------------------------------------------------------------
# clone list / status
# ---------------------------------------------------------------------------

def test_clone_list_empty(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "list"])
    assert r.exit_code == 0
    assert "No registered clones" in r.output


def test_clone_list_json_empty(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "list", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output) == []


def test_clone_status_unregistered(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "status", "github"])
    assert r.exit_code == 1
    assert "No registered clone" in r.output


def test_clone_renew_unregistered(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "renew", "github"])
    assert r.exit_code == 1


def test_clone_seed_unregistered(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "seed", "github", "small-project"])
    assert r.exit_code == 1


def test_clone_reset_unregistered(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "reset", "github"])
    assert r.exit_code == 1


def test_clone_tools_unregistered(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["clone", "tools", "github"])
    assert r.exit_code == 1
