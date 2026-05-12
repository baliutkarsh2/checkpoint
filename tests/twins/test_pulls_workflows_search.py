"""Phase 2 Plan 03: pull requests + workflows + search."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import github as gh


@pytest.fixture(autouse=True)
def _reset_state():
    gh.STATE.clear()
    gh.STATE.update(gh._fresh_state())
    gh.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(gh.app)


TOKEN = gh.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"token {TOKEN}"}


def _setup_repo_with_branch(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    main_sha = client.get("/repos/acme/webapp/branches", headers=H).json()[0]["commit"]["sha"]
    client.post(
        "/repos/acme/webapp/git/refs",
        json={"ref": "refs/heads/feature", "sha": main_sha},
        headers=H,
    )


# --- PR lifecycle --------------------------------------------------------

def test_full_pr_lifecycle(client):
    _setup_repo_with_branch(client)
    # Create PR.
    r = client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "Add feature", "head": "feature", "base": "main", "body": "yes"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    pr = r.json()
    assert pr["number"] == 1
    assert pr["state"] == "open"
    assert pr["head"]["ref"] == "feature"
    # Private fields stripped.
    assert "_commits" not in pr

    # List open PRs.
    r = client.get("/repos/acme/webapp/pulls", headers=H)
    assert len(r.json()) == 1

    # Get one.
    r = client.get("/repos/acme/webapp/pulls/1", headers=H)
    assert r.status_code == 200

    # Comment on it.
    r = client.post(
        "/repos/acme/webapp/pulls/1/comments",
        json={"body": "looks good"},
        headers=H,
    )
    assert r.status_code == 201
    r = client.get("/repos/acme/webapp/pulls/1/comments", headers=H)
    assert len(r.json()) == 1

    # Review (APPROVE).
    r = client.post(
        "/repos/acme/webapp/pulls/1/reviews",
        json={"event": "APPROVE", "body": "LGTM"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["state"] == "APPROVED"
    reviews = client.get("/repos/acme/webapp/pulls/1/reviews", headers=H).json()
    assert len(reviews) == 1

    # Update body via PATCH.
    r = client.patch(
        "/repos/acme/webapp/pulls/1",
        json={"body": "updated body"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["body"] == "updated body"

    # Merge.
    r = client.put(
        "/repos/acme/webapp/pulls/1/merge",
        json={"commit_message": "Merge feature"},
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged"] is True
    assert "sha" in body

    # State now closed.
    pr = client.get("/repos/acme/webapp/pulls/1", headers=H).json()
    assert pr["state"] == "closed"
    assert pr["merged"] is True

    # Can't merge twice.
    r = client.put(
        "/repos/acme/webapp/pulls/1/merge",
        json={},
        headers=H,
    )
    assert r.status_code == 409


def test_pr_404(client):
    r = client.get("/repos/acme/webapp/pulls/999", headers=H)
    assert r.status_code == 404


def test_list_pulls_filter_by_state_head_base(client):
    _setup_repo_with_branch(client)
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "A", "head": "feature", "base": "main"},
        headers=H,
    )
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "B", "head": "feature", "base": "main"},
        headers=H,
    )
    # Close PR #2.
    client.patch("/repos/acme/webapp/pulls/2", json={"state": "closed"}, headers=H)
    r = client.get("/repos/acme/webapp/pulls?state=open", headers=H)
    assert len(r.json()) == 1
    r = client.get("/repos/acme/webapp/pulls?state=all", headers=H)
    assert len(r.json()) == 2
    r = client.get("/repos/acme/webapp/pulls?head=feature&state=all", headers=H)
    assert len(r.json()) == 2


def test_pr_update_branch_endpoint(client):
    _setup_repo_with_branch(client)
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "x", "head": "feature", "base": "main"},
        headers=H,
    )
    before = client.get("/repos/acme/webapp/pulls/1", headers=H).json()["head"]["sha"]
    r = client.put("/repos/acme/webapp/pulls/1/update-branch", headers=H)
    assert r.status_code == 200
    after = client.get("/repos/acme/webapp/pulls/1", headers=H).json()["head"]["sha"]
    assert before != after


def test_pr_diff(client):
    _setup_repo_with_branch(client)
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "x", "head": "feature", "base": "main"},
        headers=H,
    )
    r = client.get("/repos/acme/webapp/pulls/1.diff", headers=H)
    assert r.status_code == 200
    assert "diff" in r.text


# --- workflows -----------------------------------------------------------

def test_list_and_get_workflow_runs(client):
    _setup_repo_with_branch(client)
    # Seed a workflow run into state directly (no CRUD endpoint for create per scope).
    gh.STATE["workflow_runs"]["acme/webapp#42"] = {
        "id": 42, "name": "ci", "status": "completed", "conclusion": "success",
        "head_sha": "abc123", "html_url": "http://localhost/acme/webapp/actions/runs/42",
        "created_at": "2026-05-12T00:00:00Z",
    }
    r = client.get("/repos/acme/webapp/actions/runs", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 1
    assert body["workflow_runs"][0]["id"] == 42
    # Filter by status.
    r = client.get("/repos/acme/webapp/actions/runs?status=success", headers=H)
    assert r.json()["total_count"] == 1
    r = client.get("/repos/acme/webapp/actions/runs?status=failure", headers=H)
    assert r.json()["total_count"] == 0
    # Get one.
    r = client.get("/repos/acme/webapp/actions/runs/42", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "ci"
    # 404
    r = client.get("/repos/acme/webapp/actions/runs/99", headers=H)
    assert r.status_code == 404


# --- search --------------------------------------------------------------

def test_search_code(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    import base64
    client.put(
        "/repos/acme/webapp/contents/src/app.py",
        json={"message": "init", "content": base64.b64encode(b"def hello(): return 'world'\n").decode()},
        headers=H,
    )
    r = client.get("/search/code?q=hello", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 1
    assert body["items"][0]["path"] == "src/app.py"


def test_search_users(client):
    # default-user is always seeded.
    r = client.get("/search/users?q=default", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] >= 1


def test_search_issues(client):
    client.post(
        "/repos/acme/webapp/issues",
        json={"title": "auth bug", "body": "Login broken on Safari"},
        headers=H,
    )
    client.post(
        "/repos/acme/webapp/issues",
        json={"title": "feature x", "body": "wantitnow"},
        headers=H,
    )
    r = client.get("/search/issues?q=auth", headers=H)
    assert r.status_code == 200
    assert r.json()["total_count"] == 1
