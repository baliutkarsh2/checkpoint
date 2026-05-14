"""Tests for the checkpoint web dashboard (FastAPI + Jinja2)."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from starlette.testclient import TestClient

from checkpoint.dashboard.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(run_id, scenario, sat, criteria=None):
    return {
        "run_id": run_id,
        "scenario": scenario,
        "satisfaction": sat,
        "criteria": [
            {"text": t, "kind": k, "passed": p, "reasoning": "ok", "evaluator": "regex"}
            for t, k, p in (criteria or [])
        ],
        "evaluator_model": "gpt-4o-mini",
        "env": {"timestamp": "2026-05-14T00:00:00Z"},
        "trace": [],
        "state": {},
        "exit_code": 0,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "scenarios").mkdir()
    app = create_app(runs_dir=tmp_path / "runs", scenarios_dir=tmp_path / "scenarios")
    return TestClient(app)


@pytest.fixture
def client_with_data(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()

    (runs_dir / "abc123abc123.json").write_text(json.dumps(
        _record("abc123abc123", "github-happy", 100, [("Issue exists", "D", True)])
    ))
    (runs_dir / "def456def456.json").write_text(json.dumps(
        _record("def456def456", "github-happy", 50, [("Issue exists", "D", False)])
    ))
    (scn_dir / "github-happy.md").write_text(
        "# Test\n"
        "## Prompt\nDo X\n"
        "## Success Criteria\n- [D] Exactly 1 issue exists\n"
        "## Config\nclones: github\n"
    )
    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Page routes — empty state
# ---------------------------------------------------------------------------

def test_home_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Run history" in r.text or "No runs" in r.text


def test_scenarios_empty(client):
    r = client.get("/scenarios")
    assert r.status_code == 200
    assert "Scenarios" in r.text


def test_report_empty(client):
    r = client.get("/report")
    assert r.status_code == 200
    assert "Trend report" in r.text or "Report" in r.text


# ---------------------------------------------------------------------------
# Page routes — with data
# ---------------------------------------------------------------------------

def test_home_shows_run(client_with_data):
    r = client_with_data.get("/")
    assert r.status_code == 200
    assert "abc123abc123"[:12] in r.text
    assert "github-happy" in r.text


def test_home_filter_match(client_with_data):
    r = client_with_data.get("/?scenario=github")
    assert r.status_code == 200
    assert "abc123abc123"[:12] in r.text


def test_home_filter_no_match(client_with_data):
    r = client_with_data.get("/?scenario=nonexistent")
    assert r.status_code == 200
    assert "abc123abc123" not in r.text


def test_run_detail_page(client_with_data):
    r = client_with_data.get("/runs/abc123abc123")
    assert r.status_code == 200
    assert "Issue exists" in r.text
    assert "100" in r.text


def test_run_detail_404(client_with_data):
    assert client_with_data.get("/runs/nonexistent").status_code == 404


def test_scenarios_lists_files(client_with_data):
    r = client_with_data.get("/scenarios")
    assert r.status_code == 200
    # Either the title or filename should appear
    assert "github-happy" in r.text or "Test" in r.text


def test_report_with_data(client_with_data):
    r = client_with_data.get("/report?scenario=github")
    assert r.status_code == 200
    assert "Issue exists" in r.text


def test_compare_page(client_with_data):
    r = client_with_data.get("/compare?a=abc123abc123&b=def456def456")
    assert r.status_code == 200
    assert "aseline" in r.text  # "Baseline"
    assert "andidate" in r.text  # "Candidate"


def test_compare_missing_returns_404(client_with_data):
    assert client_with_data.get("/compare?a=x&b=y").status_code == 404


def test_compare_only_one_missing(client_with_data):
    assert client_with_data.get("/compare?a=abc123abc123&b=nonexistent").status_code == 404


# ---------------------------------------------------------------------------
# API: /api/runs
# ---------------------------------------------------------------------------

def test_api_runs_empty(client):
    data = client.get("/api/runs").json()
    assert data["rows"] == []
    assert data["total"] == 0


def test_api_runs_list(client_with_data):
    data = client_with_data.get("/api/runs").json()
    assert data["total"] == 2
    assert len(data["rows"]) == 2


def test_api_runs_filter(client_with_data):
    data = client_with_data.get("/api/runs?scenario=github").json()
    assert data["total"] == 2


def test_api_runs_no_match(client_with_data):
    data = client_with_data.get("/api/runs?scenario=nope").json()
    assert data["total"] == 0
    assert data["rows"] == []


def test_api_runs_pagination(client_with_data):
    data = client_with_data.get("/api/runs?limit=1&page=1").json()
    assert len(data["rows"]) == 1
    assert data["total"] == 2


# ---------------------------------------------------------------------------
# API: /api/runs/{run_id}
# ---------------------------------------------------------------------------

def test_api_run_detail(client_with_data):
    rec = client_with_data.get("/api/runs/abc123abc123").json()
    assert rec["satisfaction"] == 100
    assert rec["scenario"] == "github-happy"


def test_api_run_detail_404(client_with_data):
    assert client_with_data.get("/api/runs/nope").status_code == 404


# ---------------------------------------------------------------------------
# API: /api/report
# ---------------------------------------------------------------------------

def test_api_report(client_with_data):
    data = client_with_data.get("/api/report?scenario=github").json()
    assert data["run_count"] == 2
    assert "criteria" in data
    assert "flaky_criteria" in data
    assert "avg_score" in data


def test_api_report_empty(client):
    data = client.get("/api/report").json()
    assert data["run_count"] == 0
    assert data["flaky_criteria"] == []


# ---------------------------------------------------------------------------
# API: /api/scenarios
# ---------------------------------------------------------------------------

def test_api_scenarios(client_with_data):
    data = client_with_data.get("/api/scenarios").json()
    assert len(data) == 1
    assert data[0]["d_count"] == 1


def test_api_scenarios_empty(client):
    data = client.get("/api/scenarios").json()
    assert data == []


# ---------------------------------------------------------------------------
# API: /api/compare
# ---------------------------------------------------------------------------

def test_api_compare(client_with_data):
    data = client_with_data.get("/api/compare?a=abc123abc123&b=def456def456").json()
    assert "regressions" in data
    assert data["baseline_score"] == 100
    assert data["candidate_score"] == 50
    assert data["delta"] == -50.0


def test_api_compare_404(client_with_data):
    assert client_with_data.get("/api/compare?a=x&b=y").status_code == 404


# ---------------------------------------------------------------------------
# API: /api/summary
# ---------------------------------------------------------------------------

def test_api_summary(client_with_data):
    data = client_with_data.get("/api/summary").json()
    assert "total_runs" in data
    assert data["total_runs"] == 2


def test_api_summary_empty(client):
    data = client.get("/api/summary").json()
    assert data["total_runs"] == 0


# ---------------------------------------------------------------------------
# API: /api/clones
# ---------------------------------------------------------------------------

def test_api_clones_no_registry(client):
    assert client.get("/api/clones").json() == []


def test_api_clones_with_registry(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    registry = tmp_path / "clones.json"
    registry.write_text(json.dumps({
        "github": {
            "pid": 1234,
            "url": "http://127.0.0.1:18001",
            "mcp_url": "http://127.0.0.1:18001/mcp/",
            "started_at": "2026-05-14T00:00:00Z",
            "port": 18001,
            "token": "x",
            "host": "127.0.0.1",
        }
    }))
    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir, clone_registry_path=registry)
    c = TestClient(app)
    data = c.get("/api/clones").json()
    assert len(data) == 1
    assert data[0]["id"] == "github"
    assert data[0]["pid"] == 1234


def test_api_clones_invalid_registry(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    registry = tmp_path / "clones.json"
    registry.write_text("not valid json")
    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir, clone_registry_path=registry)
    c = TestClient(app)
    # Should not raise — returns empty list gracefully
    assert c.get("/api/clones").json() == []
