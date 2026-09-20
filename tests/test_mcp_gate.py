"""Checkpoint over MCP: what a coding agent gets when it asks.

These pin the shape of each tool's answer, because that answer is the only thing
the calling model sees. A renamed key or a missing `evidence` string does not
raise anywhere — it just quietly stops telling the agent why a verdict came out
the way it did.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from checkpoint.mcp_gate import gate_tool, list_scenarios_tool, run_scenario_tool
from checkpoint.mcp_gate.server import build_server
from checkpoint.mcp_gate.tools import check_scenario_tool

# The bundled demo, not an example: it ships in the wheel, is scored entirely by
# assertions, and needs no API key, so these tests are hermetic.
DEMO = Path(__file__).resolve().parent.parent / "checkpoint" / "demo" / "smoke-scenario.md"
DEMO_AGENT = f"{sys.executable} -m checkpoint.demo.harness_fake"

SCENARIO = """\
---
twins: [github]
---
# Example

## Task
File an issue in acme/webapp titled "Login broken".

## Criteria
- [D] Exactly 1 issue was created
- [P] The final answer names the issue
"""


def test_list_scenarios_describes_each_one(tmp_path):
    (tmp_path / "a.md").write_text(SCENARIO, encoding="utf-8")
    listed = list_scenarios_tool(str(tmp_path))
    assert len(listed) == 1
    entry = listed[0]
    assert entry["twins"] == ["github"]
    assert entry["title"] == "Example"
    assert "Login broken" in entry["task"]
    assert [c["kind"] for c in entry["criteria"]] == ["D", "P"]


def test_list_scenarios_skips_ordinary_markdown(tmp_path):
    """A README beside the scenarios is not a scenario, and must not read as one."""
    (tmp_path / "a.md").write_text(SCENARIO, encoding="utf-8")
    (tmp_path / "README.md").write_text("# Notes\n\nSome prose.\n", encoding="utf-8")
    assert [s["title"] for s in list_scenarios_tool(str(tmp_path))] == ["Example"]


def test_list_scenarios_missing_dir_is_empty_not_an_error():
    assert list_scenarios_tool("does-not-exist") == []


def test_check_scenario_shows_the_assertion_behind_each_criterion(tmp_path):
    path = tmp_path / "a.md"
    path.write_text(SCENARIO, encoding="utf-8")
    report = check_scenario_tool(str(path))
    assert report["valid"] and report["problems"] == []
    deterministic, judged = report["criteria"]
    assert deterministic["assertion"] == "count(created.github.issues) == 1"
    # A judged criterion has no assertion, and says so rather than leaving a null
    # the calling model has to interpret.
    assert judged["assertion"] is None
    assert judged["decided_by"] == "judge"


def test_check_scenario_reports_what_would_make_a_run_meaningless(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("---\ntwins: [nosuchservice]\n---\n# X\n\n## Task\nDo it.\n", encoding="utf-8")
    report = check_scenario_tool(str(path))
    assert not report["valid"]
    assert any("nosuchservice" in p for p in report["problems"])
    assert any("Criteria" in p for p in report["problems"])


def test_server_exposes_the_four_tools():
    names = {t.name for t in asyncio.run(build_server().list_tools())}
    assert {"list_scenarios", "check_scenario", "run_scenario", "gate"} <= names


def test_run_scenario_reports_what_the_agent_did(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = run_scenario_tool(str(DEMO), DEMO_AGENT)
    assert out["error"] is None
    assert out["score"] == 100.0
    assert out["scored"] is True
    assert out["api_calls"] > 0
    assert all(c["passed"] for c in out["criteria"])
    # Each verdict carries the assertion that produced it, so the calling agent
    # can tell a real failure from a criterion that means something else.
    assert all(c["assertion"] for c in out["criteria"])


def test_gate_refuses_to_ship_on_too_little_evidence(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = gate_tool(str(DEMO), DEMO_AGENT, runs=5)
    # Five flawless runs cannot clear ship_min at any confidence. The tool says
    # so, and how many runs would, instead of handing back a soft pass.
    assert out["verdict"] == "INCONCLUSIVE"
    assert out["exit_code"] == 3
    assert out["runs_needed_to_ship"] == 16
    assert out["scenarios"][0]["pass_rate"] == 1.0
    assert "SHIP needs >= 16 clean runs" in out["scenarios"][0]["evidence"]


def test_gate_answers_a_bad_argument_instead_of_raising():
    """The caller is a model; an exception is something it can only relay."""
    out = gate_tool(str(DEMO), DEMO_AGENT, runs=0)
    assert out["verdict"] == "ERROR"
    assert out["exit_code"] == 4
    assert out["errors"]


def test_tools_refuse_clearly_when_no_agent_is_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = gate_tool(str(DEMO))
    assert out["verdict"] == "ERROR"
    assert any("checkpoint init" in e for e in out["errors"])
