"""Tests for checkpoint/analytics.py and the `checkpoint report` CLI command."""
from __future__ import annotations

import json

import pytest

from checkpoint.analytics import compute_trend, detect_flaky, load_runs_for_scenario


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(scenario: str, score: float, criteria: list[tuple[str, str, bool]]) -> dict:
    """Build a minimal synthetic run record."""
    return {
        "run_id": f"run-{scenario[:4]}-{score:.0f}",
        "scenario": scenario,
        "satisfaction": score,
        "criteria": [
            {"text": t, "kind": k, "passed": p, "reasoning": ""}
            for t, k, p in criteria
        ],
        "env": {"timestamp": "2026-05-14T00:00:00Z"},
    }


# ---------------------------------------------------------------------------
# load_runs_for_scenario
# ---------------------------------------------------------------------------

def test_filter_by_scenario_exact(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_make_run("github-happy", 100, [])))
    (tmp_path / "b.json").write_text(json.dumps(_make_run("stripe-refund", 75, [])))
    out = load_runs_for_scenario("github", tmp_path, limit=10)
    assert len(out) == 1
    assert out[0]["scenario"] == "github-happy"


def test_filter_empty_pattern_returns_all(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_make_run("github-happy", 100, [])))
    (tmp_path / "b.json").write_text(json.dumps(_make_run("stripe-refund", 75, [])))
    out = load_runs_for_scenario("", tmp_path, limit=10)
    assert len(out) == 2


def test_filter_returns_empty_when_no_match(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_make_run("github-happy", 100, [])))
    out = load_runs_for_scenario("stripe", tmp_path, limit=10)
    assert out == []


def test_filter_case_insensitive(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_make_run("GitHub-Happy", 100, [])))
    out = load_runs_for_scenario("github", tmp_path, limit=10)
    assert len(out) == 1


def test_filter_respects_limit(tmp_path):
    for i in range(5):
        (tmp_path / f"{i}.json").write_text(json.dumps(_make_run("github", 80, [])))
    out = load_runs_for_scenario("github", tmp_path, limit=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# compute_trend
# ---------------------------------------------------------------------------

def test_trend_avg_score():
    runs = [
        _make_run("s", 100, []),
        _make_run("s", 60, []),
        _make_run("s", 80, []),
    ]
    trend = compute_trend(runs)
    assert trend["avg_score"] == 80.0
    assert trend["min_score"] == 60
    assert trend["max_score"] == 100
    assert trend["run_count"] == 3


def test_trend_per_criterion_pass_rate():
    crit = "An issue titled 'X' exists"
    runs = [
        _make_run("s", 100, [(crit, "D", True)]),
        _make_run("s", 50, [(crit, "D", False)]),
        _make_run("s", 100, [(crit, "D", True)]),
    ]
    trend = compute_trend(runs)
    assert crit in trend["criteria"]
    s = trend["criteria"][crit]
    assert s["pass"] == 2
    assert s["fail"] == 1
    assert abs(s["pass_rate"] - 0.667) < 0.01


def test_trend_history_has_run_ids():
    runs = [_make_run("s", 90, []), _make_run("s", 70, [])]
    trend = compute_trend(runs)
    assert len(trend["history"]) == 2
    for h in trend["history"]:
        assert "run_id" in h
        assert "score" in h
        assert "timestamp" in h


def test_trend_empty_runs():
    trend = compute_trend([])
    assert trend["run_count"] == 0
    assert trend["avg_score"] == 0.0
    assert trend["criteria"] == {}


# ---------------------------------------------------------------------------
# detect_flaky
# ---------------------------------------------------------------------------

def test_flaky_detected():
    crit = "Some criterion"
    runs = [
        _make_run("s", 100, [(crit, "D", True)]),
        _make_run("s", 0,   [(crit, "D", False)]),
        _make_run("s", 100, [(crit, "D", True)]),
        _make_run("s", 0,   [(crit, "D", False)]),
    ]
    trend = compute_trend(runs)
    flaky = detect_flaky(trend)
    assert crit in flaky


def test_stable_pass_not_flaky():
    crit = "Always passes"
    runs = [_make_run("s", 100, [(crit, "D", True)]) for _ in range(5)]
    trend = compute_trend(runs)
    assert detect_flaky(trend) == []


def test_stable_fail_not_flaky():
    crit = "Always fails"
    runs = [_make_run("s", 0, [(crit, "D", False)]) for _ in range(5)]
    trend = compute_trend(runs)
    assert detect_flaky(trend) == []


def test_flaky_requires_min_3_runs():
    crit = "Alternating"
    runs = [
        _make_run("s", 100, [(crit, "D", True)]),
        _make_run("s", 0,   [(crit, "D", False)]),
    ]
    trend = compute_trend(runs)
    # Only 2 runs — below min threshold
    assert detect_flaky(trend) == []


# ---------------------------------------------------------------------------
# checkpoint report CLI command
# ---------------------------------------------------------------------------

def test_report_json_output(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import main

    crit = "Exactly 1 issue exists"
    for i in range(4):
        (tmp_path / f"{i}.json").write_text(
            json.dumps(_make_run("github-happy", 75, [(crit, "D", i % 2 == 0)]))
        )
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    monkeypatch.setattr("checkpoint.analytics.load_runs_for_scenario",
                        lambda pat, rd, limit: [
                            json.loads(f.read_text()) for f in tmp_path.glob("*.json")
                        ])

    runner = CliRunner()
    result = runner.invoke(main, ["report", "github-happy", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "run_count" in data
    assert "avg_score" in data
    assert "criteria" in data
    assert "flaky_criteria" in data


def test_report_no_runs_exits_0(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import main

    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["report", "nonexistent-scenario"])
    assert result.exit_code == 0
    assert "No matching" in result.output
