"""Linear twin REST surface — auth, CRUD, seed loading."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import linear as ln


@pytest.fixture(autouse=True)
def _reset_state():
    ln.STATE.clear()
    ln.STATE.update(ln._fresh_state())
    ln.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(ln.app)


TOKEN = ln.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/v1/issues")
    assert r.status_code == 401


def test_wrong_token_returns_401(client):
    r = client.get("/v1/issues", headers={"Authorization": "Bearer bad_token"})
    assert r.status_code == 401


def test_env_override_token(monkeypatch, client):
    monkeypatch.setenv("LINEAR_BOOTSTRAP_TOKEN", "lin_api_override")
    r = client.get("/v1/issues", headers=H)
    assert r.status_code == 401
    r = client.get("/v1/issues", headers={"Authorization": "Bearer lin_api_override"})
    assert r.status_code == 200


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


# --- organization -----------------------------------------------------------

def test_get_organization(client):
    r = client.get("/v1/organization", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "id" in body
    assert "name" in body


# --- teams ------------------------------------------------------------------

def test_list_teams_returns_default_team(client):
    r = client.get("/v1/teams", headers=H)
    body = r.json()
    assert isinstance(body["nodes"], list)
    assert len(body["nodes"]) >= 1
    assert body["nodes"][0]["key"] == "ENG"


def test_get_team_by_id(client):
    r = client.get("/v1/teams/team-engineering", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == "team-engineering"


def test_get_team_not_found(client):
    r = client.get("/v1/teams/team-nope", headers=H)
    assert r.status_code == 404


def test_create_team(client):
    r = client.post("/v1/teams", headers=H, json={"name": "Frontend", "key": "FE"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Frontend"
    assert body["key"] == "FE"
    assert "id" in body


# --- workflow states --------------------------------------------------------

def test_list_workflow_states(client):
    r = client.get("/v1/workflow-states", headers=H)
    body = r.json()
    assert len(body["nodes"]) >= 1
    names = [s["name"] for s in body["nodes"]]
    assert "Backlog" in names or "Todo" in names


def test_list_workflow_states_filter_team(client):
    r = client.get("/v1/workflow-states?teamId=team-engineering", headers=H)
    assert r.status_code == 200
    assert r.json()["nodes"]


# --- issues -----------------------------------------------------------------

def test_create_issue(client):
    r = client.post("/v1/issues", headers=H, json={
        "title": "Fix the bug",
        "teamId": "team-engineering",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Fix the bug"
    assert "id" in body
    assert body["state"]["type"] in ("backlog", "unstarted")


def test_create_issue_requires_title(client):
    r = client.post("/v1/issues", headers=H, json={"teamId": "team-engineering"})
    assert r.status_code in (400, 422)


def test_list_issues_empty(client):
    r = client.get("/v1/issues", headers=H)
    assert r.json()["nodes"] == []


def test_list_issues_filter_by_team(client):
    client.post("/v1/issues", headers=H, json={"title": "A", "teamId": "team-engineering"})
    client.post("/v1/issues", headers=H, json={"title": "B", "teamId": "team-engineering"})
    r = client.get("/v1/issues?teamId=team-engineering", headers=H)
    assert len(r.json()["nodes"]) == 2


def test_get_issue(client):
    body = client.post("/v1/issues", headers=H, json={"title": "X", "teamId": "team-engineering"}).json()
    r = client.get(f"/v1/issues/{body['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["title"] == "X"


def test_get_issue_not_found(client):
    assert client.get("/v1/issues/nope", headers=H).status_code == 404


def test_update_issue_title(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "Old", "teamId": "team-engineering"}).json()
    r = client.patch(f"/v1/issues/{iss['id']}", headers=H, json={"title": "New"})
    assert r.status_code == 200
    assert r.json()["title"] == "New"


def test_update_issue_state(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"}).json()
    states = client.get("/v1/workflow-states", headers=H).json()["nodes"]
    in_prog = next((s for s in states if s["type"] == "started"), None)
    if in_prog:
        r = client.patch(f"/v1/issues/{iss['id']}", headers=H, json={"stateId": in_prog["id"]})
        assert r.status_code == 200
        assert r.json()["state"]["id"] == in_prog["id"]


def test_close_issue_via_patch(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"}).json()
    states = client.get("/v1/workflow-states", headers=H).json()["nodes"]
    done = next((s for s in states if s["type"] == "completed"), None)
    assert done, "Fresh state should have a 'Done' workflow state"
    r = client.patch(f"/v1/issues/{iss['id']}", headers=H, json={"stateId": done["id"]})
    assert r.status_code == 200
    assert r.json()["state"]["type"] == "completed"


def test_delete_issue(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"}).json()
    r = client.delete(f"/v1/issues/{iss['id']}", headers=H)
    assert r.status_code in (200, 204)
    # Linear archives (sets archivedAt) rather than hard-deleting
    body = r.json()
    assert body.get("success") is True or ln.STATE["issues"][iss["id"]].get("archivedAt")


# --- comments ---------------------------------------------------------------

def test_add_comment_to_issue(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"}).json()
    r = client.post(f"/v1/issues/{iss['id']}/comments", headers=H, json={"body": "Great issue!"})
    assert r.status_code == 201
    assert r.json()["body"] == "Great issue!"


def test_list_comments(client):
    iss = client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"}).json()
    client.post(f"/v1/issues/{iss['id']}/comments", headers=H, json={"body": "c1"})
    client.post(f"/v1/issues/{iss['id']}/comments", headers=H, json={"body": "c2"})
    r = client.get(f"/v1/issues/{iss['id']}/comments", headers=H)
    assert len(r.json()["nodes"]) == 2


# --- projects ---------------------------------------------------------------

def test_create_and_get_project(client):
    r = client.post("/v1/projects", headers=H, json={
        "name": "Q3 Roadmap",
        "teamIds": ["team-engineering"],
    })
    assert r.status_code == 201
    proj = r.json()
    assert proj["name"] == "Q3 Roadmap"
    r2 = client.get(f"/v1/projects/{proj['id']}", headers=H)
    assert r2.status_code == 200


def test_list_projects(client):
    client.post("/v1/projects", headers=H, json={"name": "P1", "teamIds": ["team-engineering"]})
    r = client.get("/v1/projects", headers=H)
    assert len(r.json()["nodes"]) == 1


# --- labels -----------------------------------------------------------------

def test_create_and_list_labels(client):
    r = client.post("/v1/labels", headers=H, json={"name": "bug", "color": "#ff0000"})
    assert r.status_code == 201
    assert r.json()["name"] == "bug"
    r2 = client.get("/v1/labels", headers=H)
    assert any(lab["name"] == "bug" for lab in r2.json()["nodes"])


# --- cycles -----------------------------------------------------------------

def test_create_and_list_cycles(client):
    r = client.post("/v1/cycles", headers=H, json={
        "name": "Sprint 1",
        "teamId": "team-engineering",
        "startsAt": "2026-01-01T00:00:00Z",
        "endsAt": "2026-01-14T00:00:00Z",
    })
    assert r.status_code == 201
    cycle = r.json()
    assert cycle["name"] == "Sprint 1"
    r2 = client.get("/v1/cycles", headers=H)
    assert len(r2.json()["nodes"]) == 1


# --- seed loading -----------------------------------------------------------

def test_seed_small_project(client):
    r = client.post("/_seed/small-project")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["issues"]
    assert state["projects"]


def test_seed_sprint_planning(client):
    r = client.post("/_seed/sprint-planning")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["cycles"]
    assert state["issues"]


def test_seed_unknown_returns_404(client):
    r = client.post("/_seed/nonexistent-seed")
    assert r.status_code == 404


def test_reset_clears_state(client):
    client.post("/_seed/small-project")
    client.post("/_reset")
    state = client.get("/_state").json()
    assert not state["issues"]


# --- trace ------------------------------------------------------------------

def test_trace_records_requests(client):
    client.post("/v1/issues", headers=H, json={"title": "T", "teamId": "team-engineering"})
    trace = client.get("/_trace").json()
    assert len(trace) >= 1
    assert any(e["path"].startswith("/v1/issues") for e in trace)
