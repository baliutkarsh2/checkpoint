"""OpenTelemetry GenAI span ingestion into the trajectory model."""
from __future__ import annotations

import json

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.trajectory import compute_metrics, from_otel_spans

_SPANS = [
    {"name": "chat", "attributes": {"gen_ai.request.model": "gpt-4o"}},
    {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "get_order"}},
    {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "get_order"},
     "status": {"code": "ERROR"}},   # redundant + error
    {"name": "GET /health"},          # non-GenAI span, ignored
]


def test_from_simple_spans():
    traj = from_otel_spans(_SPANS)
    assert len(traj) == 3  # the non-GenAI span is dropped
    assert traj.steps[0].method == "LLM" and traj.steps[0].path == "gpt-4o"
    assert traj.steps[1].method == "TOOL" and traj.steps[1].path == "get_order"

    m = compute_metrics(traj)
    assert m.total_calls == 3
    assert m.error_calls == 1        # the ERROR-status tool span
    assert m.redundant_calls == 1    # get_order twice


def test_from_otlp_json_attributes():
    span = {"name": "execute_tool", "attributes": [
        {"key": "gen_ai.tool.name", "value": {"stringValue": "issue_refund"}},
    ]}
    traj = from_otel_spans([span])
    assert traj.steps[0].path == "issue_refund"


def test_otel_cli_list_form(tmp_path):
    f = tmp_path / "spans.json"
    f.write_text(json.dumps(_SPANS))
    r = CliRunner().invoke(main, ["otel", str(f), "-o", "json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["steps"] == 3
    assert payload["metrics"]["error_calls"] == 1
    assert "TOOL get_order" in payload["path"]


def test_otel_cli_full_otlp_structure(tmp_path):
    otlp = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"name": "chat", "attributes": [
            {"key": "gen_ai.request.model", "value": {"stringValue": "claude-3-5"}}]},
    ]}]}]}
    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(otlp))
    r = CliRunner().invoke(main, ["otel", str(f), "-o", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["steps"] == 1
