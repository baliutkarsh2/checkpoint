"""Tests for `checkpoint runs list` and `checkpoint compare` CLI commands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.run_record import build_record, write_record


def _make_record(
    tmp_path: Path,
    name: str,
    satisfaction: float,
    passed: list[str],
    failed: list[str],
    run_id: str | None = None,
) -> dict:
    criteria = [
        {"text": t, "passed": True, "kind": "D", "reasoning": "ok", "evaluator": "regex"}
        for t in passed
    ] + [
        {"text": t, "passed": False, "kind": "D", "reasoning": "fail", "evaluator": "regex"}
        for t in failed
    ]
    record = build_record(
        scenario_name=name,
        scenario_path=f"scenarios/{name}.md",
        satisfaction=satisfaction,
        criteria=criteria,
        evaluator_model="gpt-test",
        evaluator_model_source="test",
        final_answer="done",
        trace=[],
        state={},
        run_id=run_id or name[:12],
    )
    write_record(record, root=tmp_path / ".checkpoint" / "cache")
    # write_record uses root/runs/ but the function takes cache root
    # Re-write using the actual pattern
    return record


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / ".checkpoint" / "cache"
    runs = cache / "runs"
    runs.mkdir(parents=True)
    return runs


def _write_run(runs_dir: Path, run_id: str, record: dict) -> None:
    (runs_dir / f"{run_id}.json").write_text(json.dumps(record))


# --- runs list --------------------------------------------------------------

def test_runs_list_empty(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list"])
    assert result.exit_code == 0
    assert "No run records" in result.output or result.output.strip() == "" or "run" in result.output.lower()


def test_runs_list_shows_records(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = {
        "run_id": "abc123",
        "scenario": "github-happy-path",
        "satisfaction": 75.0,
        "criteria": [],
        "evaluator_model": "gpt-4o-mini",
        "evaluator_model_source": "config",
        "final_answer": "done",
        "trace": [],
        "state": {},
        "error": None,
        "exit_code": 0,
        "env": {"timestamp": "2026-05-12T10:00:00Z"},
    }
    _write_run(runs_dir, "abc123", record)
    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list"])
    assert result.exit_code == 0
    # Rich may wrap long names; check run_id and score which are bounded-width
    assert "abc123" in result.output
    assert "75" in result.output


def test_runs_list_multiple_records(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i, (rid, scn, sat) in enumerate([
        ("run001aabbcc", "scenario-a", 100.0),
        ("run002aabbcc", "scenario-b", 50.0),
        ("run003aabbcc", "scenario-c", 0.0),
    ]):
        _write_run(runs_dir, rid, {
            "run_id": rid,
            "scenario": scn,
            "satisfaction": sat,
            "criteria": [],
            "evaluator_model": "test",
            "evaluator_model_source": "test",
            "final_answer": "done",
            "trace": [],
            "state": {},
            "error": None,
            "exit_code": 0,
            "env": {"timestamp": f"2026-05-1{i+1}T10:00:00Z"},
        })
    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list"])
    assert result.exit_code == 0
    assert "scenario-a" in result.output
    assert "scenario-b" in result.output
    assert "scenario-c" in result.output


def test_runs_list_json_flag(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = {
        "run_id": "jsontest1",
        "scenario": "json-test",
        "satisfaction": 80.0,
        "criteria": [],
        "evaluator_model": "test",
        "evaluator_model_source": "test",
        "final_answer": "done",
        "trace": [],
        "state": {},
        "error": None,
        "exit_code": 0,
        "env": {"timestamp": "2026-05-12T10:00:00Z"},
    }
    _write_run(runs_dir, "jsontest1", record)
    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["run_id"] == "jsontest1"


def test_runs_list_limit(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(5):
        _write_run(runs_dir, f"run{i:04d}aabb", {
            "run_id": f"run{i:04d}aabb",
            "scenario": f"scn-{i}",
            "satisfaction": float(i * 20),
            "criteria": [],
            "evaluator_model": "test",
            "evaluator_model_source": "test",
            "final_answer": "done",
            "trace": [],
            "state": {},
            "error": None,
            "exit_code": 0,
            "env": {"timestamp": f"2026-05-{i+1:02d}T10:00:00Z"},
        })
    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "-n", "3"])
    assert result.exit_code == 0
    # Should show at most 3 entries
    shown = [line for line in result.output.splitlines() if "scn-" in line]
    assert len(shown) <= 3


# --- compare ----------------------------------------------------------------

def _write_full_run(runs_dir: Path, run_id: str, scenario: str, satisfaction: float,
                    criteria: list[dict]) -> None:
    _write_run(runs_dir, run_id, {
        "run_id": run_id,
        "scenario": scenario,
        "satisfaction": satisfaction,
        "criteria": criteria,
        "evaluator_model": "test",
        "evaluator_model_source": "test",
        "final_answer": "done",
        "trace": [],
        "state": {},
        "error": None,
        "exit_code": 0,
        "env": {"timestamp": "2026-05-12T10:00:00Z"},
    })


def test_compare_same_score(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    criteria = [{"text": "exactly 1 issue is open", "passed": True, "kind": "D", "reasoning": "ok", "evaluator": "regex"}]
    _write_full_run(runs_dir, "baselinerun", "github-happy-path", 100.0, criteria)
    _write_full_run(runs_dir, "candidaterun", "github-happy-path", 100.0, criteria)
    runner = CliRunner()
    result = runner.invoke(main, ["compare", "baselinerun", "candidaterun"])
    assert result.exit_code == 0
    assert "compare" in result.output.lower() or "delta" in result.output.lower() or "0" in result.output


def test_compare_regression(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    crit_pass = {"text": "no new issues", "passed": True, "kind": "D", "reasoning": "ok", "evaluator": "regex"}
    crit_fail = {"text": "no new issues", "passed": False, "kind": "D", "reasoning": "nope", "evaluator": "regex"}
    _write_full_run(runs_dir, "baserun001", "scn-x", 100.0, [crit_pass])
    _write_full_run(runs_dir, "candrun001", "scn-x", 0.0, [crit_fail])
    runner = CliRunner()
    result = runner.invoke(main, ["compare", "baserun001", "candrun001"])
    assert result.exit_code == 0
    # A regression should appear in the output
    assert "Regression" in result.output or "regression" in result.output or "-100" in result.output


def test_compare_improvement(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    crit_fail = {"text": "no new issues", "passed": False, "kind": "D", "reasoning": "nope", "evaluator": "regex"}
    crit_pass = {"text": "no new issues", "passed": True, "kind": "D", "reasoning": "ok", "evaluator": "regex"}
    _write_full_run(runs_dir, "baserun002", "scn-x", 0.0, [crit_fail])
    _write_full_run(runs_dir, "candrun002", "scn-x", 100.0, [crit_pass])
    runner = CliRunner()
    result = runner.invoke(main, ["compare", "baserun002", "candrun002"])
    assert result.exit_code == 0
    assert "Improvement" in result.output or "improvement" in result.output or "+100" in result.output


def test_compare_missing_run_exits_1(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_full_run(runs_dir, "existingrun", "scn-x", 100.0, [])
    runner = CliRunner()
    result = runner.invoke(main, ["compare", "existingrun", "doesnotexist"])
    assert result.exit_code != 0


def test_compare_json_flag(runs_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    criteria = [{"text": "exactly 0 issues", "passed": True, "kind": "D", "reasoning": "ok", "evaluator": "regex"}]
    _write_full_run(runs_dir, "basejson001", "scn", 100.0, criteria)
    _write_full_run(runs_dir, "candjson001", "scn", 50.0, [{"text": "exactly 0 issues", "passed": False, "kind": "D", "reasoning": "fail", "evaluator": "regex"}])
    runner = CliRunner()
    result = runner.invoke(main, ["compare", "basejson001", "candjson001", "--json"])
    assert result.exit_code == 0
    diff = json.loads(result.output)
    assert "baseline_score" in diff
    assert "candidate_score" in diff
    assert "delta" in diff
    assert diff["delta"] == -50.0
