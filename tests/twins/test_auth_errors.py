"""Phase 2 Plan 01: bootstrap-token auth + GitHub error/header shapes."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import github as gh


@pytest.fixture(autouse=True)
def _reset_state():
    # Hard reset between tests.
    gh.STATE.clear()
    gh.STATE.update(gh._fresh_state())
    gh.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(gh.app)


TOKEN = gh.DEFAULT_BOOTSTRAP_TOKEN


def test_missing_authorization_returns_401(client):
    r = client.get("/repos/acme/webapp")
    assert r.status_code == 401
    body = r.json()
    assert body["message"] == "Bad credentials"
    assert "documentation_url" in body


def test_wrong_token_returns_401(client):
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": "token ghp_wrongtoken"},
    )
    assert r.status_code == 401
    assert r.json()["message"] == "Bad credentials"


def test_bearer_form_accepted(client):
    # Repo doesn't exist yet so we expect 404, NOT 401 — token was accepted.
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 404
    assert r.json()["message"] == "Not Found"


def test_token_form_accepted(client):
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": f"token {TOKEN}"},
    )
    assert r.status_code == 404


def test_env_override(monkeypatch, client):
    monkeypatch.setenv("GITHUB_BOOTSTRAP_TOKEN", "ghp_envoverride")
    # Default token is now wrong.
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": f"token {TOKEN}"},
    )
    assert r.status_code == 401
    # The overridden token works.
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": "token ghp_envoverride"},
    )
    assert r.status_code == 404


def test_introspection_endpoints_bypass_auth(client):
    # Each /_* endpoint should respond without an Authorization header.
    assert client.get("/_health").status_code == 200
    assert client.get("/_trace").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


def test_introspection_not_in_trace(client):
    client.get("/_health")
    client.get("/_state")
    trace = client.get("/_trace").json()
    assert trace == []
    # A non-introspection call should land in trace.
    client.get("/repos/x/y", headers={"Authorization": f"token {TOKEN}"})
    trace = client.get("/_trace").json()
    assert len(trace) == 1
    assert trace[0]["path"] == "/repos/x/y"


def test_response_headers_are_github_shape(client):
    r = client.get("/_health")
    # health doesn't get GH headers (intro bypass) -- but check on a real endpoint
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": f"token {TOKEN}"},
    )
    assert "X-GitHub-Media-Type" in r.headers
    assert r.headers["X-GitHub-Media-Type"].startswith("github.")
    assert "X-GitHub-Request-Id" in r.headers
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers


def test_error_body_has_documentation_url(client):
    r = client.get(
        "/repos/acme/webapp",
        headers={"Authorization": f"token {TOKEN}"},
    )
    assert r.status_code == 404
    body = r.json()
    assert body["message"] == "Not Found"
    assert body["documentation_url"].startswith("https://docs.github.com")


def test_existing_issue_flow_still_works_with_auth(client):
    h = {"Authorization": f"token {TOKEN}"}
    r = client.post(
        "/repos/acme/webapp/issues",
        json={"title": "hello world", "body": "hi"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    issue = r.json()
    assert issue["number"] == 1
    # Listing returns it.
    r = client.get("/repos/acme/webapp/issues", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 1
