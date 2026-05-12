"""Phase 2 Plan 02: repos / branches / files / commits surface."""
from __future__ import annotations

import base64

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


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# --- repos ---------------------------------------------------------------

def test_create_user_repo(client):
    r = client.post("/user/repos", json={"name": "webapp"}, headers=H)
    assert r.status_code == 201
    repo = r.json()
    assert repo["full_name"] == "default-user/webapp"
    assert repo["default_branch"] == "main"
    assert "main" in repo["branches"]
    # Initial commit was created.
    assert len(repo["commits"]) == 1


def test_get_repo_404(client):
    r = client.get("/repos/no/such", headers=H)
    assert r.status_code == 404
    assert r.json()["message"] == "Not Found"


def test_search_repositories(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.post("/user/repos", json={"name": "api", "owner": "acme"}, headers=H)
    client.post("/user/repos", json={"name": "cli", "owner": "globex"}, headers=H)
    r = client.get("/search/repositories?q=acme", headers=H)
    assert r.status_code == 200
    payload = r.json()
    assert payload["total_count"] == 2
    names = {i["full_name"] for i in payload["items"]}
    assert names == {"acme/webapp", "acme/api"}


def test_fork_repository(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.post(
        "/repos/acme/webapp/forks",
        json={"organization": "myfork"},
        headers=H,
    )
    assert r.status_code == 202
    fork = r.json()
    assert fork["full_name"] == "myfork/webapp"
    assert fork["fork"] is True
    assert fork["parent"]["full_name"] == "acme/webapp"


# --- files / contents ----------------------------------------------------

def test_create_or_update_file_and_get_contents(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.put(
        "/repos/acme/webapp/contents/README.md",
        json={"message": "Add readme", "content": _b64("Hello\n")},
        headers=H,
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["commit"]["commit"]["message"] == "Add readme"

    r = client.get("/repos/acme/webapp/contents/README.md", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "README.md"
    decoded = base64.b64decode(body["content"]).decode()
    assert decoded == "Hello\n"


def test_get_file_contents_404(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.get("/repos/acme/webapp/contents/missing.txt", headers=H)
    assert r.status_code == 404


def test_push_files_batch(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.post(
        "/repos/acme/webapp/_push_files",
        json={
            "branch": "main",
            "message": "Add scaffolding",
            "files": [
                {"path": "src/index.py", "content": "print('hi')\n"},
                {"path": "tests/test_x.py", "content": "def test_x(): pass\n"},
            ],
        },
        headers=H,
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["files_pushed"] == 2
    assert payload["commit"]["commit"]["message"] == "Add scaffolding"
    # Both files now retrievable.
    r1 = client.get("/repos/acme/webapp/contents/src/index.py", headers=H)
    assert r1.status_code == 200
    r2 = client.get("/repos/acme/webapp/contents/tests/test_x.py", headers=H)
    assert r2.status_code == 200


# --- branches ------------------------------------------------------------

def test_list_branches_starts_with_main(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.get("/repos/acme/webapp/branches", headers=H)
    assert r.status_code == 200
    branches = r.json()
    assert len(branches) == 1
    assert branches[0]["name"] == "main"
    assert branches[0]["protected"] is False


def test_create_and_delete_branch(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    # Need the main sha to base the branch on.
    main_sha = client.get(
        "/repos/acme/webapp/branches", headers=H
    ).json()[0]["commit"]["sha"]
    r = client.post(
        "/repos/acme/webapp/git/refs",
        json={"ref": "refs/heads/feature", "sha": main_sha},
        headers=H,
    )
    assert r.status_code == 201, r.text
    # Now 2 branches.
    branches = client.get("/repos/acme/webapp/branches", headers=H).json()
    assert {b["name"] for b in branches} == {"main", "feature"}
    # Delete the feature branch.
    r = client.delete("/repos/acme/webapp/git/refs/heads/feature", headers=H)
    assert r.status_code == 204
    branches = client.get("/repos/acme/webapp/branches", headers=H).json()
    assert {b["name"] for b in branches} == {"main"}


def test_delete_default_branch_refused(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.delete("/repos/acme/webapp/git/refs/heads/main", headers=H)
    assert r.status_code == 422


def test_create_branch_duplicate(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    r = client.post(
        "/repos/acme/webapp/git/refs",
        json={"ref": "refs/heads/main"},
        headers=H,
    )
    assert r.status_code == 422


# --- commits -------------------------------------------------------------

def test_list_commits_after_push(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.post(
        "/repos/acme/webapp/_push_files",
        json={
            "branch": "main",
            "message": "Add code",
            "files": [{"path": "a.py", "content": "x = 1\n"}],
        },
        headers=H,
    )
    r = client.get("/repos/acme/webapp/commits", headers=H)
    assert r.status_code == 200
    commits = r.json()
    # initial + push
    assert len(commits) == 2
    assert commits[0]["commit"]["message"] == "Add code"
    assert commits[1]["commit"]["message"] == "Initial commit"


def test_list_commits_path_filter(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.post(
        "/repos/acme/webapp/_push_files",
        json={"branch": "main", "message": "A", "files": [{"path": "a.py", "content": ""}]},
        headers=H,
    )
    client.post(
        "/repos/acme/webapp/_push_files",
        json={"branch": "main", "message": "B", "files": [{"path": "b.py", "content": ""}]},
        headers=H,
    )
    r = client.get("/repos/acme/webapp/commits?path=a.py", headers=H)
    assert r.status_code == 200
    commits = r.json()
    assert len(commits) == 1
    assert commits[0]["commit"]["message"] == "A"
