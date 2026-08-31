"""Checkpoint-as-MCP-server: the tool functions and server construction."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from checkpoint.mcp_gate import gate_tool, list_scenarios_tool, run_scenario_tool
from checkpoint.mcp_gate.server import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "examples" / "smoke" / "smoke-scenario.md"
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"


def test_list_scenarios_tool(tmp_path):
    (tmp_path / "a.md").write_text(
        "# a\n## Prompt\ndo a thing\n## Success Criteria\n- [D] x\n## Config\nclones: github\n"
    )
    out = list_scenarios_tool(str(tmp_path))
    assert len(out) == 1
    assert out[0]["clones"] == ["github"]
    assert out[0]["criteria"] == 1
    assert "do a thing" in out[0]["prompt"]


def test_list_scenarios_missing_dir():
    assert list_scenarios_tool("does-not-exist") == []


def test_build_server_exposes_tools():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"list_scenarios", "run_scenario", "gate"} <= names


@pytest.mark.skipif(not (SMOKE.is_file() and FAKE_HARNESS.is_file()), reason="smoke assets missing")
def test_run_scenario_tool_end_to_end(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = run_scenario_tool(str(SMOKE), f"{sys.executable} {FAKE_HARNESS}")
    assert out["error"] is None
    assert out["score"] == 100.0
    assert out["criteria"] and out["criteria"][0]["passed"] is True


@pytest.mark.skipif(not (SMOKE.is_file() and FAKE_HARNESS.is_file()), reason="smoke assets missing")
def test_gate_tool_end_to_end(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = gate_tool(str(SMOKE), f"{sys.executable} {FAKE_HARNESS}", runs=5)
    assert out["verdict"] in ("SHIP", "CONDITIONAL")
    assert out["scenarios"][0]["pass_rate"] == 1.0
