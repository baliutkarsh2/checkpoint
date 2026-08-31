"""User config (~/.checkpoint/config.json) actually affects `run` and `serve`."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from checkpoint.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "examples" / "smoke" / "smoke-scenario.md"
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"


def _write_config(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_serve_uses_config_port_and_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    _write_config(tmp_path, {"dashboard": {"port": 5599, "host": "127.0.0.1"}})

    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))

    result = CliRunner().invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    assert captured.get("port") == 5599
    assert captured.get("host") == "127.0.0.1"


def test_serve_flag_overrides_config_port(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    _write_config(tmp_path, {"dashboard": {"port": 5599}})
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    result = CliRunner().invoke(main, ["serve", "--port", "6000"])
    assert result.exit_code == 0, result.output
    assert captured.get("port") == 6000  # explicit flag wins


def test_run_uses_config_pass_threshold(tmp_path, monkeypatch):
    """A pass_threshold from user config gates the exit code even with no flag."""
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
        import pytest
        pytest.skip("smoke assets missing")
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # The smoke scenario scores 100; an impossible threshold must fail the run.
    _write_config(tmp_path, {"defaults": {"pass_threshold": 101}})

    import sys

    result = CliRunner().invoke(main, [
        "run", str(SMOKE), "--no-docker",
        "--harness", f"{sys.executable} {FAKE_HARNESS}",
    ])
    # Threshold 101 not met by a score of 100 -> non-zero exit.
    assert result.exit_code != 0, result.output
