"""Security regressions for the dashboard (PR-2): no RCE, no path traversal."""
from __future__ import annotations

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from checkpoint.dashboard.app import create_app


@pytest.fixture
def app_dirs(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    scn = tmp_path / "scenarios"
    scn.mkdir()
    (scn / "ok.md").write_text("# ok\n## Prompt\ndo nothing\n")
    return tmp_path, runs, scn


def _client(app_dirs):
    tmp_path, runs, scn = app_dirs
    return TestClient(create_app(runs_dir=runs, scenarios_dir=scn, project_dir=tmp_path))


def test_jobs_rejects_unknown_harness_no_rce(app_dirs):
    """A free-form command must never reach the subprocess: an unknown harness
    reference is rejected instead of run."""
    c = _client(app_dirs)
    r = c.post("/api/jobs", json={
        "scenario": "ok.md",
        "docker": False,
        "harness": "python -c \"import os; os.system('touch pwned')\"",
    })
    assert r.status_code == 400
    assert "unknown harness" in r.json()["detail"].lower()


def test_jobs_rejects_out_of_tree_scenario(app_dirs):
    """An absolute scenario path outside the project is rejected."""
    tmp_path, _, _ = app_dirs
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# x\n## Prompt\nx\n")
    c = _client(app_dirs)
    r = c.post("/api/jobs", json={"scenario": str(outside)})
    assert r.status_code == 400


def test_scenarios_listing_traversal_rejected(app_dirs):
    c = _client(app_dirs)
    for bad in ("../..", "../../etc", "/etc"):
        r = c.get("/api/scenarios", params={"path": bad})
        assert r.status_code == 400, f"{bad!r} should be rejected"


def test_scenarios_listing_ok_without_path(app_dirs):
    c = _client(app_dirs)
    r = c.get("/api/scenarios")
    assert r.status_code == 200


def test_serve_refuses_non_loopback_without_key(monkeypatch):
    """`checkpoint serve --host 0.0.0.0` must refuse without an API key."""
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    from checkpoint.cli import main

    result = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "without authentication" in result.output.lower()


def test_serve_allows_non_loopback_with_key(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-key")
    import uvicorn

    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.setdefault("ran", True))
    from checkpoint.cli import main

    result = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0"])
    # It should get past the auth gate to the uvicorn.run call.
    assert called.get("ran") is True, result.output
