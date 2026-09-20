"""GitHub twin: seeds, rate limiting and permission denials."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import github as gh

SEED_NAMES = [
    "empty", "small-project", "enterprise-repo", "stale-issues",
    "large-backlog", "merge-conflict", "ci-cd-pipeline",
    "permissions-denied", "rate-limited",
]

TOKEN = gh.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"token {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_state():
    gh.STATE.clear()
    gh.STATE.update(gh._fresh_state())
    gh.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(gh.app)


@pytest.mark.parametrize("seed", SEED_NAMES)
def test_seed_loads(client, seed):
    r = client.post(f"/_seed/{seed}")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["seed"] == seed


def test_seed_404(client):
    r = client.post("/_seed/does-not-exist")
    assert r.status_code == 404


def test_small_project_has_two_repos_and_issues(client):
    client.post("/_seed/small-project")
    r = client.get("/repos/acme/webapp", headers=H)
    assert r.status_code == 200
    assert r.json()["full_name"] == "acme/webapp"
    issues = client.get("/repos/acme/webapp/issues?state=all", headers=H).json()
    assert len(issues) == 2
    titles = {i["title"] for i in issues}
    assert "Add dark mode" in titles


def test_ci_cd_pipeline_lists_workflow_runs(client):
    client.post("/_seed/ci-cd-pipeline")
    r = client.get("/repos/acme/webapp/actions/runs", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 3
    ids = {run["id"] for run in body["workflow_runs"]}
    assert ids == {100, 101, 102}


def test_permissions_denied_seed_blocks_writes(client):
    client.post("/_seed/permissions-denied")
    # GET works.
    r = client.get("/repos/acme/webapp", headers=H)
    assert r.status_code == 200
    # POST blocked.
    r = client.post(
        "/repos/acme/webapp/issues",
        json={"title": "x"},
        headers=H,
    )
    assert r.status_code == 403
    body = r.json()
    assert body["message"] == "Resource not accessible by integration"
    # PATCH blocked.
    r = client.patch("/repos/acme/webapp/issues/1", json={"state": "closed"}, headers=H)
    assert r.status_code == 403
    # DELETE blocked.
    r = client.delete("/repos/acme/webapp/git/refs/heads/main", headers=H)
    assert r.status_code == 403


def test_rate_limited_seed_returns_429_after_n_requests(client):
    client.post("/_seed/rate-limited")  # rate_limit = 5
    # 5 successful requests.
    for _ in range(5):
        r = client.get("/repos/acme/webapp", headers=H)
        assert r.status_code == 200, r.text
    # 6th: 429
    r = client.get("/repos/acme/webapp", headers=H)
    assert r.status_code == 429
    body = r.json()
    assert "rate limit" in body["message"].lower()
    assert "documentation_url" in body
    # Rate-limit headers present, and the reset is a real epoch a few seconds
    # out: an SDK that waits for it (PyGithub does) has to be able to recover.
    assert "X-RateLimit-Limit" in r.headers
    assert r.headers["X-RateLimit-Limit"] == "5"
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert 0 < int(r.headers["Retry-After"]) <= gh.RATE_LIMIT_RESET_S
    reset = int(r.headers["X-RateLimit-Reset"])
    assert time.time() < reset <= time.time() + gh.RATE_LIMIT_RESET_S + 1


def test_rate_limit_window_refills(client):
    """The budget comes back at the advertised reset, so a backing-off agent progresses."""
    client.post("/_reset")  # the request counter lives on the twin, not in state
    client.post("/_config", json={"rate_limit": 1})
    assert client.get("/repos/x/y", headers=H).status_code == 404
    assert client.get("/repos/x/y", headers=H).status_code == 429
    time.sleep(gh.RATE_LIMIT_RESET_S + 0.2)
    assert client.get("/repos/x/y", headers=H).status_code == 404


def test_config_endpoint_tweaks_runtime(client):
    # Set rate limit to 2 dynamically.
    r = client.post("/_config", json={"rate_limit": 2})
    assert r.status_code == 200
    # 2 ok, 3rd 429.
    client.get("/repos/x/y", headers=H)
    client.get("/repos/x/y", headers=H)
    r = client.get("/repos/x/y", headers=H)
    assert r.status_code == 429
    # Clear it.
    client.post("/_config", json={"rate_limit": None})
    # Reset counter manually via /_reset so we're not stuck post-cap.
    client.post("/_reset")
    client.post("/_config", json={"rate_limit": None})
    r = client.get("/repos/x/y", headers=H)
    assert r.status_code == 404  # 404 = repo missing; auth and rate-limit fine


def test_reset_clears_state_and_counters(client):
    client.post("/_seed/small-project")
    # 2 issues exist.
    issues = client.get("/repos/acme/webapp/issues?state=all", headers=H).json()
    assert len(issues) == 2
    client.post("/_reset")
    # The seeded repo is gone, so its issues are a 404 — not an empty list.
    assert client.get("/repos/acme/webapp/issues?state=all", headers=H).status_code == 404
    assert gh.STATE["issues"] == {} and gh.STATE["repos"] == {}


def test_seeded_numbers_continue_after_the_seed(client):
    """A new issue must not reuse a seeded number, or the agent overwrites history."""
    client.post("/_seed/small-project")
    created = client.post("/repos/acme/webapp/issues", json={"title": "new"}, headers=H)
    assert created.json()["number"] == 3
    comment = client.post("/repos/acme/webapp/issues/3/comments", json={"body": "hi"},
                          headers=H)
    assert comment.json()["id"] == 2  # the seed already holds comment 1


def test_seeded_issue_labels_exist_on_the_repository(client):
    client.post("/_seed/small-project")
    names = {lab["name"] for lab in client.get("/repos/acme/webapp/labels", headers=H).json()}
    assert {"bug", "enhancement", "in-progress"} <= names
