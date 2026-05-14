"""Tests for checkpoint.sdk — the public Python API."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from checkpoint.sdk import (
    Checkpoint,
    CriterionSpec,
    RunConfig,
    TwinHandle,
)
from checkpoint.runner import RunResult, CriterionResult


# ---------------------------------------------------------------------------
# CriterionSpec / RunConfig dataclasses
# ---------------------------------------------------------------------------

def test_criterion_spec_defaults():
    c = CriterionSpec(text="At least 1 issue exists")
    assert c.kind == "D"
    assert c.text == "At least 1 issue exists"


def test_criterion_spec_explicit_kind():
    c = CriterionSpec(text="Issue was handled well", kind="P")
    assert c.kind == "P"


def test_run_config_defaults():
    cfg = RunConfig()
    assert cfg.clones == ["github"]
    assert cfg.seed is None
    assert cfg.evaluator_model == "gpt-4o-mini"
    assert cfg.timeout == 120
    assert cfg.cwd is None
    assert cfg.harness_cmd == [sys.executable, "harness.py"]


def test_run_config_custom():
    cfg = RunConfig(
        clones=["github", "slack"],
        seed="small-project",
        harness_cmd=["python", "my_agent.py"],
        evaluator_model="gpt-4o",
        timeout=60,
        cwd="/tmp/work",
    )
    assert cfg.clones == ["github", "slack"]
    assert cfg.seed == "small-project"
    assert cfg.evaluator_model == "gpt-4o"
    assert cfg.timeout == 60


# ---------------------------------------------------------------------------
# Checkpoint.run_scenario
# ---------------------------------------------------------------------------

def _make_run_result(**overrides) -> RunResult:
    defaults = dict(
        final_answer="Done",
        stderr="",
        exit_code=0,
        trace=[],
        state={},
        criteria=[
            CriterionResult(
                text="At least 1 issue exists",
                kind="D",
                passed=True,
                reasoning="1 issue found",
                evaluator="deterministic",
            )
        ],
        error=None,
    )
    defaults.update(overrides)
    return RunResult(**defaults)


def test_run_scenario_calls_run_once(tmp_path, monkeypatch):
    """run_scenario() builds a Scenario and delegates to run_once."""
    mock_result = _make_run_result()
    monkeypatch.setattr("checkpoint.sdk.run_once", lambda **kw: mock_result)
    monkeypatch.setattr("checkpoint.sdk.write_record", lambda r, **kw: None)
    monkeypatch.setattr("checkpoint.sdk.build_record", lambda **kw: {})

    client = Checkpoint()
    result = client.run_scenario(
        title="Test scenario",
        prompt="Create an issue",
        criteria=[CriterionSpec("At least 1 issue exists")],
        config=RunConfig(clones=["github"], harness_cmd=["python", "harness.py"]),
    )

    assert result is mock_result
    assert result.score == 100.0


def test_run_scenario_scenario_shape(tmp_path, monkeypatch):
    """run_scenario() passes correctly shaped Scenario to run_once."""
    captured: dict = {}

    def fake_run_once(*, scenario, harness_cmd, cwd, judge_model):
        captured["scenario"] = scenario
        captured["harness_cmd"] = harness_cmd
        captured["judge_model"] = judge_model
        return _make_run_result()

    monkeypatch.setattr("checkpoint.sdk.run_once", fake_run_once)
    monkeypatch.setattr("checkpoint.sdk.write_record", lambda r, **kw: None)
    monkeypatch.setattr("checkpoint.sdk.build_record", lambda **kw: {})

    client = Checkpoint()
    client.run_scenario(
        title="My test",
        prompt="Do the thing",
        criteria=[
            CriterionSpec("At least 1 issue exists", kind="D"),
            CriterionSpec("Agent was helpful", kind="P"),
        ],
        config=RunConfig(
            clones=["github", "slack"],
            seed="small-project",
            evaluator_model="gpt-4o",
        ),
    )

    scn = captured["scenario"]
    assert scn.title == "My test"
    assert scn.prompt == "Do the thing"
    assert len(scn.criteria) == 2
    assert scn.criteria[0].kind == "D"
    assert scn.criteria[1].kind == "P"
    assert scn.clones == ["github", "slack"]
    assert scn.config.get("seed") == "small-project"
    assert captured["judge_model"] == "gpt-4o"


def test_run_scenario_writes_record(tmp_path, monkeypatch):
    """run_scenario() calls write_record with the built record."""
    written: list[dict] = []
    fake_record = {"run_id": "abc123"}

    monkeypatch.setattr("checkpoint.sdk.run_once", lambda **kw: _make_run_result())
    monkeypatch.setattr("checkpoint.sdk.build_record", lambda **kw: fake_record)
    monkeypatch.setattr("checkpoint.sdk.write_record", lambda r, **kw: written.append(r))

    client = Checkpoint()
    client.run_scenario(
        title="T",
        prompt="P",
        criteria=[CriterionSpec("At least 1 issue exists")],
    )

    assert len(written) == 1
    assert written[0] is fake_record


# ---------------------------------------------------------------------------
# Checkpoint.run_file
# ---------------------------------------------------------------------------

def test_run_file_loads_scenario(tmp_path, monkeypatch):
    """run_file() parses the .md file and passes its Scenario to run_once."""
    scenario_file = tmp_path / "test.md"
    scenario_file.write_text(
        "# My scenario\n\n"
        "## Prompt\nCreate issue\n\n"
        "## Success Criteria\n"
        "- [D] At least 1 issue exists\n\n"
        "## Config\nclones: github\n",
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_run_once(*, scenario, harness_cmd, cwd, judge_model):
        captured["scenario"] = scenario
        return _make_run_result()

    monkeypatch.setattr("checkpoint.sdk.run_once", fake_run_once)
    monkeypatch.setattr("checkpoint.sdk.write_record", lambda r, **kw: None)
    monkeypatch.setattr("checkpoint.sdk.build_record", lambda **kw: {})

    client = Checkpoint()
    result = client.run_file(scenario_file)

    assert result.score == 100.0
    assert captured["scenario"].title == "My scenario"
    assert captured["scenario"].prompt == "Create issue"
    assert len(captured["scenario"].criteria) == 1


# ---------------------------------------------------------------------------
# Checkpoint.twin_session
# ---------------------------------------------------------------------------

def _make_clone_entry(clone_id: str, port: int = 9999) -> dict:
    return {
        "pid": 12345,
        "port": port,
        "host": "127.0.0.1",
        "started_at": "2026-05-13T00:00:00Z",
        "url": f"http://127.0.0.1:{port}",
        "mcp_url": f"http://127.0.0.1:{port}/mcp/",
        "token": "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt",
    }


def test_twin_session_yields_handles(tmp_path, monkeypatch):
    """twin_session() yields TwinHandle dicts and stops on exit."""
    stopped: list[str] = []

    monkeypatch.setattr(
        "checkpoint.clone_manager.start",
        lambda clone_id, **kw: _make_clone_entry(clone_id),
    )
    monkeypatch.setattr(
        "checkpoint.clone_manager.stop",
        lambda clone_id, **kw: stopped.append(clone_id) or True,
    )

    client = Checkpoint()
    registry = tmp_path / "reg.json"

    with client.twin_session(["github"], registry_path=registry) as twins:
        assert "github" in twins
        gh = twins["github"]
        assert isinstance(gh, TwinHandle)
        assert gh.clone_id == "github"
        assert "127.0.0.1" in gh.url
        assert gh.mcp_url.endswith("/mcp/")

    assert "github" in stopped


def test_twin_session_stops_on_error(tmp_path, monkeypatch):
    """twin_session() stops already-started twins even if a later start fails."""
    stopped: list[str] = []
    call_count = {"n": 0}

    def fake_start(clone_id, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("Failed to start slack")
        return _make_clone_entry(clone_id)

    monkeypatch.setattr("checkpoint.clone_manager.start", fake_start)
    monkeypatch.setattr(
        "checkpoint.clone_manager.stop",
        lambda clone_id, **kw: stopped.append(clone_id) or True,
    )

    client = Checkpoint()
    registry = tmp_path / "reg.json"

    with pytest.raises(RuntimeError, match="Failed to start slack"):
        with client.twin_session(["github", "slack"], registry_path=registry):
            pass

    assert "github" in stopped


def test_twin_session_multi_clone(tmp_path, monkeypatch):
    """twin_session() returns handles for all requested clones."""
    ports = {"github": 9001, "slack": 9002}

    monkeypatch.setattr(
        "checkpoint.clone_manager.start",
        lambda clone_id, **kw: _make_clone_entry(clone_id, ports[clone_id]),
    )
    monkeypatch.setattr("checkpoint.clone_manager.stop", lambda *a, **kw: True)

    client = Checkpoint()
    registry = tmp_path / "reg.json"

    with client.twin_session(["github", "slack"], registry_path=registry) as twins:
        assert set(twins.keys()) == {"github", "slack"}
        assert "9001" in twins["github"].url
        assert "9002" in twins["slack"].url
