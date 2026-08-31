"""CLI-07: clone start/stop/inspect round trip."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from checkpoint import clone_manager
from checkpoint.cli import main
from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN


def test_start_inspect_stop_github(tmp_path):
    reg = tmp_path / "clones.json"

    # start
    entry = clone_manager.start("github", registry_path=reg)
    try:
        assert entry["pid"] > 0
        assert entry["url"].startswith("http://127.0.0.1:")
        assert entry["mcp_url"].endswith("/mcp/")
        assert entry["token"].startswith("ghp_")
        assert reg.exists()
        on_disk = json.loads(reg.read_text())
        assert "github" in on_disk

        # inspect — should be alive, state keys populated
        info = clone_manager.inspect("github", registry_path=reg)
        assert info is not None
        assert info["alive"] is True
        assert "repositories" in info["state_keys"] or info["state_keys"]
        assert info["request_count"] >= 0
    finally:
        # stop
        was_running = clone_manager.stop("github", registry_path=reg)
        assert was_running is True

    # registry cleared
    assert json.loads(reg.read_text()) == {}

    # inspect after stop returns None
    info2 = clone_manager.inspect("github", registry_path=reg)
    assert info2 is None


def test_start_rejects_unknown_clone(tmp_path):
    reg = tmp_path / "clones.json"
    with pytest.raises(ValueError):
        clone_manager.start("salesforce", registry_path=reg)


def test_start_rejects_duplicate_running(tmp_path):
    reg = tmp_path / "clones.json"
    clone_manager.start("github", registry_path=reg)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            clone_manager.start("github", registry_path=reg)
    finally:
        clone_manager.stop("github", registry_path=reg)


def test_inspect_purges_stale_pid(tmp_path):
    """If the registry has an entry with a dead PID, inspect purges it."""
    reg = tmp_path / "clones.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({
        "github": {
            "pid": 999999,  # unlikely-to-exist PID
            "port": 9999,
            "host": "127.0.0.1",
            "started_at": "2020-01-01T00:00:00Z",
            "url": "http://127.0.0.1:9999",
            "mcp_url": "http://127.0.0.1:9999/mcp/",
            "token": FAKE_GITHUB_TOKEN,
        }
    }))
    info = clone_manager.inspect("github", registry_path=reg)
    assert info is not None
    assert info["alive"] is False
    # registry should be purged
    assert json.loads(reg.read_text()) == {}


def test_stop_unknown_clone_returns_false(tmp_path):
    reg = tmp_path / "clones.json"
    assert clone_manager.stop("github", registry_path=reg) is False


def test_cli_start_inspect_stop(tmp_path, monkeypatch):
    """End-to-end via the Click runner. Uses cwd-relative default registry."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        r = runner.invoke(main, ["clone", "start", "github"])
        assert r.exit_code == 0, r.output
        assert "URL:" in r.output
        assert "MCP URL:" in r.output
        assert "Token:" in r.output

        r2 = runner.invoke(main, ["clone", "inspect", "github"])
        assert r2.exit_code == 0, r2.output
        assert "alive" in r2.output
    finally:
        r3 = runner.invoke(main, ["clone", "stop", "github"])
        assert r3.exit_code == 0, r3.output
        assert "Stopped" in r3.output


def test_cli_inspect_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(main, ["clone", "inspect", "github"])
    assert r.exit_code == 1
    assert "No registered clone" in r.output
