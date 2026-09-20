"""The dashboard can start agent processes, so reaching it must stay hard.

Two ways that goes wrong, both pinned here: a request body that turns
POST /api/jobs into a command runner, and a path parameter that reads outside
the project. Plus the bind guard — off loopback the dashboard is remote code
execution for anyone who can reach the port, so it refuses to start without a
key.
"""
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


@pytest.mark.parametrize("field", ["harness", "command", "agent", "cmd", "extra_args"])
def test_jobs_cannot_be_told_which_command_to_run(app_dirs, field):
    """A free-form command must never reach the subprocess.

    The agent comes from checkpoint.toml, so there is no field a caller can put
    one in — and an unrecognized field is refused rather than ignored, which is
    what stops a renamed option from quietly becoming a passthrough.
    """
    tmp_path, _, _ = app_dirs
    payload = "python -c \"import os; os.system('touch pwned')\""
    r = _client(app_dirs).post("/api/jobs", json={"scenario": "ok.md", field: payload})
    assert r.status_code == 422, f"{field!r} was accepted: {r.text}"
    assert not (tmp_path / "pwned").exists()


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


def _uvicorn_calls(monkeypatch) -> list[dict]:
    """Record what `checkpoint view` would have bound, without binding it."""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_view_refuses_to_bind_off_loopback_without_a_key(host, tmp_path, monkeypatch):
    """The dashboard runs agent commands on request: no key, no public port."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    calls = _uvicorn_calls(monkeypatch)
    from checkpoint.cli import main

    result = CliRunner().invoke(main, ["view", "--host", host])
    assert result.exit_code == 1
    assert "without authentication" in result.output.lower()
    assert calls == [], "it must refuse before anything starts listening"


def test_view_binds_off_loopback_once_a_key_is_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECKPOINT_DASHBOARD_API_KEY", "secret-key")
    calls = _uvicorn_calls(monkeypatch)
    from checkpoint.cli import main

    result = CliRunner().invoke(main, ["view", "--host", "0.0.0.0", "--port", "4123"])
    assert calls and calls[0]["host"] == "0.0.0.0", result.output
    assert calls[0]["port"] == 4123


def test_view_needs_no_key_on_loopback(tmp_path, monkeypatch):
    """The guard is about who can reach the port, not about locking out the user."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHECKPOINT_DASHBOARD_API_KEY", raising=False)
    calls = _uvicorn_calls(monkeypatch)
    from checkpoint.cli import main

    result = CliRunner().invoke(main, ["view"])
    assert calls and calls[0]["host"] == "127.0.0.1", result.output
