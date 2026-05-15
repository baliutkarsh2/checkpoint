"""Tests for the cloud-deploy hardening middlewares.

BearerAuthMiddleware  — kicks in when CHECKPOINT_DASHBOARD_API_KEY is set
ReadOnlyJobsMiddleware — kicks in when CHECKPOINT_DASHBOARD_READ_ONLY=1
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from checkpoint.dashboard.app import create_app


@pytest.fixture
def empty_dirs(tmp_path):
    (tmp_path / "runs").mkdir()
    scn = tmp_path / "scenarios"
    scn.mkdir()
    # Drop a real scenario file so POST /api/jobs's path-resolution step
    # can find something — these tests only care about middleware behaviour,
    # not the actual run.
    (scn / "x.md").write_text(
        "# x\n## Prompt\nnoop\n## Success Criteria\n- [D] anything\n"
        "## Config\nclones: github\n",
        encoding="utf-8",
    )
    return tmp_path


def _client(tmp_path):
    return TestClient(create_app(
        runs_dir=tmp_path / "runs",
        scenarios_dir=tmp_path / "scenarios",
    ))


# ---------------------------------------------------------------------------
# BearerAuthMiddleware
# ---------------------------------------------------------------------------

def test_no_auth_required_when_api_key_unset(empty_dirs, monkeypatch):
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    c = _client(empty_dirs)
    # POST without any auth header succeeds (with whatever validation Pydantic does).
    r = c.post("/api/jobs", json={"scenario": "x.md"})
    assert r.status_code in (201, 422)  # 422 only if validation fails — auth is fine


def test_writes_require_bearer_when_api_key_set(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    c = _client(empty_dirs)
    r = c.post("/api/jobs", json={"scenario": "x.md"})
    assert r.status_code == 401
    assert "bearer" in r.json()["error"].lower()
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


def test_writes_pass_with_correct_bearer(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    c = _client(empty_dirs)
    r = c.post(
        "/api/jobs",
        json={"scenario": "x.md"},
        headers={"Authorization": "Bearer secret-token-xyz"},
    )
    assert r.status_code in (201, 422)  # 422 = validation, not auth


def test_writes_reject_wrong_bearer(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    c = _client(empty_dirs)
    r = c.post(
        "/api/jobs",
        json={"scenario": "x.md"},
        headers={"Authorization": "Bearer WRONG-TOKEN"},
    )
    assert r.status_code == 401


def test_reads_open_by_default_with_api_key(empty_dirs, monkeypatch):
    """When AUTH_READS isn't set, GETs to /api/* stay public for SPA loads."""
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_AUTH_READS", raising=False)
    c = _client(empty_dirs)
    r = c.get("/api/runs")
    assert r.status_code == 200


def test_reads_require_bearer_when_auth_reads_set(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_AUTH_READS", "1")
    c = _client(empty_dirs)
    r = c.get("/api/runs")
    assert r.status_code == 401
    r = c.get("/api/runs", headers={"Authorization": "Bearer secret-token-xyz"})
    assert r.status_code == 200


def test_reads_accept_query_param_token(empty_dirs, monkeypatch):
    """SSE / EventSource can't set headers — fall back to ?key=<token>."""
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_AUTH_READS", "1")
    c = _client(empty_dirs)
    r = c.get("/api/runs?key=secret-token-xyz")
    assert r.status_code == 200


def test_healthz_metrics_docs_always_public(empty_dirs, monkeypatch):
    """Load balancers + scrapers + bookmarks need these paths regardless."""
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-token-xyz")
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_AUTH_READS", "1")
    c = _client(empty_dirs)
    for path in ("/healthz", "/metrics", "/api/openapi.json"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} should be public"


# ---------------------------------------------------------------------------
# ReadOnlyJobsMiddleware
# ---------------------------------------------------------------------------

def test_jobs_post_blocked_in_read_only_mode(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_READ_ONLY", "1")
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    c = _client(empty_dirs)
    r = c.post("/api/jobs", json={"scenario": "x.md"})
    assert r.status_code == 403
    assert "read-only" in r.json()["error"].lower()


def test_jobs_get_still_works_in_read_only_mode(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_READ_ONLY", "1")
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    c = _client(empty_dirs)
    r = c.get("/api/jobs")
    assert r.status_code == 200


def test_jobs_delete_blocked_in_read_only_mode(empty_dirs, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_READ_ONLY", "1")
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    c = _client(empty_dirs)
    r = c.delete("/api/jobs/anything")
    assert r.status_code == 403


def test_other_writes_unaffected_by_read_only(empty_dirs, monkeypatch):
    """Read-only specifically targets jobs; other write endpoints (none today
    but we test the boundary) shouldn't be blocked."""
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_READ_ONLY", "1")
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    c = _client(empty_dirs)
    # /api/runs is GET-only — POST should 405, NOT 403 from our middleware.
    r = c.post("/api/runs", json={})
    assert r.status_code != 403
