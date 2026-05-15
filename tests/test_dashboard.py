"""Tests for the checkpoint web dashboard (FastAPI JSON API + SPA serve)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
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
# Health, metrics, meta
# ---------------------------------------------------------------------------

def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_metrics_exposed(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "checkpoint_uptime_seconds" in r.text
    assert "checkpoint_http_requests_total" in r.text


def test_api_meta(client_with_data):
    m = client_with_data.get("/api/meta").json()
    assert "version" in m
    assert "runs_dir" in m
    assert "scenarios_dir" in m
    assert m["judge_model_default"] == "gpt-4o-mini"


def test_request_id_propagated(client):
    r = client.get("/healthz", headers={"x-request-id": "deadbeef"})
    assert r.headers.get("x-request-id") == "deadbeef"


def test_request_id_minted_when_absent(client):
    r = client.get("/healthz")
    assert r.headers.get("x-request-id")
    assert len(r.headers["x-request-id"]) >= 8


# ---------------------------------------------------------------------------
# OpenAPI docs
# ---------------------------------------------------------------------------

def test_openapi_docs_served(client):
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/docs").status_code == 200


# ---------------------------------------------------------------------------
# Runs API
# ---------------------------------------------------------------------------

def test_api_runs_empty(client):
    data = client.get("/api/runs").json()
    assert data["rows"] == []
    assert data["total"] == 0


def test_api_runs_returns_summary_shape(client_with_data):
    data = client_with_data.get("/api/runs").json()
    assert data["total"] == 2
    row = data["rows"][0]
    expected = {
        "run_id", "scenario", "scenario_path", "satisfaction",
        "criteria_pass", "criteria_total", "evaluator_model",
        "timestamp", "exit_code",
        # v0.2 additions — agent identity + duration. None for older records.
        "harness_name", "harness_dir", "mode", "duration_ms",
    }
    assert expected == set(row.keys())
    assert row["criteria_pass"] in (0, 1)
    assert row["criteria_total"] == 1


def test_api_runs_filter(client_with_data):
    data = client_with_data.get("/api/runs?scenario=github").json()
    assert data["total"] == 2


def test_api_runs_no_match(client_with_data):
    data = client_with_data.get("/api/runs?scenario=nope").json()
    assert data["total"] == 0


def test_api_runs_pagination(client_with_data):
    data = client_with_data.get("/api/runs?per_page=1&page=1").json()
    assert len(data["rows"]) == 1
    assert data["total"] == 2
    assert data["per_page"] == 1


def test_api_run_detail_returns_full_record(client_with_data):
    rec = client_with_data.get("/api/runs/abc123abc123").json()
    assert rec["satisfaction"] == 100
    assert rec["scenario"] == "github-happy"
    assert isinstance(rec["criteria"], list)
    assert isinstance(rec["trace"], list)


def test_api_run_detail_404(client_with_data):
    assert client_with_data.get("/api/runs/nope").status_code == 404


# ---------------------------------------------------------------------------
# Summary, Report, Scenarios, Compare
# ---------------------------------------------------------------------------

def test_api_summary(client_with_data):
    data = client_with_data.get("/api/summary").json()
    assert data["total_runs"] == 2
    assert "avg_score_30d" in data


def test_api_report(client_with_data):
    data = client_with_data.get("/api/report?scenario=github").json()
    assert data["run_count"] == 2
    assert "criteria" in data
    assert "flaky_criteria" in data


def test_api_scenarios_returns_coverage(client_with_data):
    data = client_with_data.get("/api/scenarios").json()
    assert "scenarios" in data
    assert "coverage" in data
    assert len(data["scenarios"]) == 1
    assert data["scenarios"][0]["d_count"] == 1
    assert data["coverage"]["total_d"] == 1


def test_api_scenarios_empty(client):
    data = client.get("/api/scenarios").json()
    assert data["scenarios"] == []
    assert data["coverage"]["total_d"] == 0


def test_api_compare(client_with_data):
    data = client_with_data.get("/api/compare?a=abc123abc123&b=def456def456").json()
    assert "rec_a" in data
    assert "rec_b" in data
    assert "diff" in data
    assert data["diff"]["baseline_score"] == 100
    assert data["diff"]["candidate_score"] == 50
    assert data["diff"]["delta"] == -50.0


def test_api_compare_404(client_with_data):
    assert client_with_data.get("/api/compare?a=x&b=y").status_code == 404


# ---------------------------------------------------------------------------
# Clones
# ---------------------------------------------------------------------------

def test_api_clones_no_registry(client):
    assert client.get("/api/clones").json() == []


def test_api_clones_with_registry(tmp_path):
    runs_dir = tmp_path / "runs"; runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"; scn_dir.mkdir()
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


def test_api_clones_invalid_registry(tmp_path):
    runs_dir = tmp_path / "runs"; runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"; scn_dir.mkdir()
    registry = tmp_path / "clones.json"
    registry.write_text("not valid json")
    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir, clone_registry_path=registry)
    c = TestClient(app)
    assert c.get("/api/clones").json() == []


# ---------------------------------------------------------------------------
# Jobs API
# ---------------------------------------------------------------------------

def test_jobs_list_empty(client):
    assert client.get("/api/jobs").json() == []


def test_jobs_get_404(client):
    assert client.get("/api/jobs/nonexistent").status_code == 404


def test_jobs_post_rejects_extra_args(client):
    """Regression: extra_args used to flow into the spawned subprocess CLI.
    The schema must now reject any unknown field so a `--host 0.0.0.0`
    developer cannot turn this into a flag-injection vector."""
    r = client.post(
        "/api/jobs",
        json={"scenario": "x.md", "extra_args": ["--harness", "pwned"]},
    )
    assert r.status_code == 422  # pydantic rejects unknown fields


def test_jobs_post_rejects_arbitrary_extra_field(client):
    r = client.post(
        "/api/jobs",
        json={"scenario": "x.md", "judge": "evil-model"},
    )
    assert r.status_code == 422


def test_jobs_start_and_lifecycle(tmp_path, monkeypatch):
    """Smoke-test job creation. Use a no-op command so we don't actually run
    the full checkpoint pipeline in the unit tests — just verify the manager
    spawns a process and tracks status."""
    runs_dir = tmp_path / "runs"; runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"; scn_dir.mkdir()
    (scn_dir / "noop.md").write_text("# noop\n## Prompt\ndo nothing\n")

    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir, project_dir=tmp_path)

    # Replace the cmd so we just exit 0 quickly without running the real CLI.
    import sys as _sys
    from checkpoint.dashboard import jobs as jobs_mod

    real_start = jobs_mod.JobManager.start

    async def fake_start(self, scenario, **kw):
        # Keep all the original wiring but swap the cmd to one that exits clean.
        job = await real_start(self, scenario, **kw)
        job.cmd = [_sys.executable, "-c", "print('hello'); print('done')"]
        return job

    monkeypatch.setattr(jobs_mod.JobManager, "start", fake_start)

    with TestClient(app) as c:
        r = c.post("/api/jobs", json={"scenario": "noop.md"})
        assert r.status_code == 201
        job = r.json()
        assert job["status"] in ("queued", "running", "succeeded", "failed")
        # Wait briefly for the spawned task to complete.
        for _ in range(40):
            time.sleep(0.05)
            cur = c.get(f"/api/jobs/{job['job_id']}").json()
            if cur["status"] in ("succeeded", "failed", "cancelled"):
                break
        assert cur["status"] in ("succeeded", "failed")  # both acceptable in CI
        listed = c.get("/api/jobs").json()
        assert any(j["job_id"] == job["job_id"] for j in listed)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_burst_writes(tmp_path):
    runs_dir = tmp_path / "runs"; runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"; scn_dir.mkdir()
    app = create_app(runs_dir=runs_dir, scenarios_dir=scn_dir, project_dir=tmp_path)

    # 30 writes/10s is the cap. The 31st in a tight loop must 429.
    with TestClient(app) as c:
        statuses = []
        for _ in range(35):
            r = c.post("/api/jobs", json={"scenario": "missing.md"})
            statuses.append(r.status_code)
        assert 429 in statuses


def test_rate_limit_does_not_block_reads(client):
    for _ in range(50):
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# SPA fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def spa_app(tmp_path):
    """App backed by a fake SPA bundle so we can test the catch-all."""
    runs_dir = tmp_path / "runs"; runs_dir.mkdir()
    scn_dir = tmp_path / "scenarios"; scn_dir.mkdir()
    static = Path(__file__).resolve().parents[1] / "checkpoint" / "dashboard" / "static"
    if not (static / "index.html").exists():
        pytest.skip("SPA bundle not built")
    return create_app(runs_dir=runs_dir, scenarios_dir=scn_dir)


def test_spa_index_served_at_root(spa_app):
    c = TestClient(spa_app)
    r = c.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text


def test_spa_fallback_for_deep_links(spa_app):
    c = TestClient(spa_app)
    r = c.get("/runs/anything/here")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text


def test_spa_does_not_swallow_api(spa_app):
    c = TestClient(spa_app)
    # /api/runs/xyz should 404 (the API), not return SPA index.
    r = c.get("/api/runs/nope")
    assert r.status_code == 404
    assert r.headers.get("content-type", "").startswith("application/json")


# ---------------------------------------------------------------------------
# SSE event bus — unit-tested directly so we don't depend on TestClient's
# streaming behaviour, which deadlocks on Windows for long-poll endpoints.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_bus_publish_subscribe():
    from checkpoint.dashboard.events import EventBus
    bus = EventBus()
    q = await bus.subscribe()
    await bus.publish("run.created", {"run_id": "x"})
    evt = await asyncio.wait_for(q.get(), timeout=1.0)
    assert evt.name == "run.created"
    assert evt.data == {"run_id": "x"}
    await bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_event_bus_drops_slow_subscriber():
    """Slow subscribers must not backpressure publishers."""
    from checkpoint.dashboard.events import EventBus
    bus = EventBus()
    bus.QUEUE_MAX = 2  # type: ignore[misc]
    await bus.subscribe()
    # Fill the queue past its max — third publish must drop the slow consumer.
    for i in range(5):
        await bus.publish("test", {"i": i})
    # Subscriber count drops to 0 after overflow.
    assert bus.subscriber_count == 0


def test_events_endpoint_registered(client):
    """The endpoint should exist and accept GET — actually consuming the stream
    requires async I/O which TestClient's sync API can't do safely."""
    routes = [r.path for r in client.app.routes]  # type: ignore[attr-defined]
    assert "/api/events" in routes
