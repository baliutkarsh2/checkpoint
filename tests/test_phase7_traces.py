"""CLI-06: checkpoint traces detail/export."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from checkpoint import run_record as rr
from checkpoint.cli import main


def _make_record(run_id: str = "abcd12345678") -> dict:
    return {
        "run_id": run_id,
        "scenario": "test scenario",
        "scenario_path": "/tmp/scn.md",
        "satisfaction": 80.0,
        "criteria": [
            {"text": "exactly 1 issue exists", "kind": "D",
             "passed": True, "reasoning": "matched regex",
             "evaluator": "deterministic"},
            {"text": "the agent was kind", "kind": "P",
             "passed": False, "reasoning": "tone was rude",
             "evaluator": "llm"},
        ],
        "evaluator_model": "gpt-4o-mini",
        "evaluator_model_source": "default",
        "failure_analysis": {"the agent was kind": "Trace entry #3 used 'no.'"},
        "final_answer": "ok",
        "stdout": "full stdout",
        "stderr": "",
        "agent_trace": {"messages": [{"role": "assistant", "content": "ok"}]},
        "trace": [],
        "state": {"repositories": [], "issues": [{"number": 1}]},
        "error": None,
        "exit_code": 0,
        "env": {"timestamp": "2026-05-12T00:00:00Z"},
    }


def test_traces_detail_uses_last_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _make_record()
    rr.write_record(rec)

    runner = CliRunner()
    result = runner.invoke(main, ["traces", "detail"])
    assert result.exit_code == 0, result.output
    assert rec["run_id"] in result.output
    assert "test scenario" in result.output
    # Satisfaction printed
    assert "80" in result.output


def test_traces_detail_by_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _make_record(run_id="zzz999888777")
    rr.write_record(rec)

    runner = CliRunner()
    result = runner.invoke(main, ["traces", "detail", "zzz999888777"])
    assert result.exit_code == 0, result.output
    assert "zzz999888777" in result.output


def test_traces_detail_missing_id_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["traces", "detail", "nope000nope0"])
    assert result.exit_code == 1
    assert "No run record" in result.output


def test_traces_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _make_record()
    rr.write_record(rec)

    out = tmp_path / "export.json"
    runner = CliRunner()
    result = runner.invoke(main, ["traces", "export", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded["run_id"] == rec["run_id"]
    assert loaded["satisfaction"] == 80.0


def test_traces_telemetry_prints_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _make_record()
    rr.write_record(rec)

    runner = CliRunner()
    result = runner.invoke(main, ["traces", "telemetry"])
    assert result.exit_code == 0, result.output
    assert "Telemetry" in result.output
    assert "agent messages" in result.output


def test_traces_telemetry_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = _make_record()
    rr.write_record(rec)

    runner = CliRunner()
    result = runner.invoke(main, ["traces", "telemetry", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["chat"]["messages"][0]["content"] == "ok"
    assert data["transcript"]["stdout"] == "full stdout"


def test_telemetry_api_call_body_fallback():
    """B4: twins record payloads under `body`/`response`; the normalizer must
    surface them as `request_body`/`response_body`."""
    from checkpoint.telemetry import build_telemetry_report

    rec = _make_record()
    rec["trace"] = [
        {  # twin-shaped event
            "method": "POST",
            "path": "/repos/a/b/issues",
            "body": {"title": "bug"},
            "response": {"number": 7},
            "status": 201,
            "ts": "2026-05-12T00:00:01Z",
        },
        {  # explicit keys still win over the fallback
            "method": "GET",
            "path": "/x",
            "request_body": {"q": 1},
            "response_body": {"ok": True},
            "body": {"shadowed": True},
            "response": {"shadowed": True},
            "status": 200,
        },
    ]
    report = build_telemetry_report(rec)
    calls = report["api_calls"]
    assert calls[0]["request_body"] == {"title": "bug"}
    assert calls[0]["response_body"] == {"number": 7}
    assert calls[0]["timestamp"] == "2026-05-12T00:00:01Z"
    assert calls[1]["request_body"] == {"q": 1}
    assert calls[1]["response_body"] == {"ok": True}
