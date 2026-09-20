"""GitHub twin: the repos / branches / files / commits surface."""
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
    assert repo["owner"]["login"] == "default-user"
    # Git state lives in the twin, not on the wire (GitHub's repo payload has no
    # branches or commits in it).
    stored = gh.STATE["repos"]["default-user/webapp"]
    assert "main" in stored["branches"]
    assert len(stored["commits"]) == 1


def test_create_repo_with_auto_init_has_a_readme(client):
    client.post("/user/repos", json={"name": "webapp", "auto_init": True}, headers=H)
    r = client.get("/repos/default-user/webapp/contents/README.md", headers=H)
    assert r.status_code == 200
    assert base64.b64decode(r.json()["content"]).decode() == "# webapp\n"


def test_get_repo_404(client):
    r = client.get("/repos/no/such", headers=H)
    assert r.status_code == 404
    assert r.json()["message"] == "Not Found"


def test_writes_to_a_missing_repo_404_instead_of_creating_it(client):
    """The twin used to conjure the repo, which hid a bad repo name from the agent."""
    assert client.post("/repos/acme/ghost/issues", json={"title": "x"},
                       headers=H).status_code == 404
    assert client.get("/repos/acme/ghost/issues", headers=H).status_code == 404
    assert client.put("/repos/acme/ghost/contents/a.txt",
                      json={"message": "m", "content": _b64("x")},
                      headers=H).status_code == 404
    assert gh.STATE["repos"] == {}


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


def test_contents_are_per_branch(client):
    """A commit on a branch must not be visible on the default branch."""
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    main_sha = client.get("/repos/acme/webapp/branches", headers=H).json()[0]["commit"]["sha"]
    client.post("/repos/acme/webapp/git/refs",
                json={"ref": "refs/heads/feature", "sha": main_sha}, headers=H)
    client.put("/repos/acme/webapp/contents/src/app.py",
               json={"message": "feat", "content": _b64("x = 1\n"), "branch": "feature"},
               headers=H)
    assert client.get("/repos/acme/webapp/contents/src/app.py?ref=feature",
                      headers=H).status_code == 200
    assert client.get("/repos/acme/webapp/contents/src/app.py", headers=H).status_code == 404


def test_update_file_needs_the_current_sha(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.put("/repos/acme/webapp/contents/a.txt",
               json={"message": "add", "content": _b64("one\n")}, headers=H)
    sha = client.get("/repos/acme/webapp/contents/a.txt", headers=H).json()["sha"]
    # Overwriting without a sha is rejected, a stale sha conflicts.
    assert client.put("/repos/acme/webapp/contents/a.txt",
                      json={"message": "x", "content": _b64("two\n")},
                      headers=H).status_code == 422
    assert client.put("/repos/acme/webapp/contents/a.txt",
                      json={"message": "x", "content": _b64("two\n"), "sha": "0" * 40},
                      headers=H).status_code == 409
    r = client.put("/repos/acme/webapp/contents/a.txt",
                   json={"message": "x", "content": _b64("two\n"), "sha": sha}, headers=H)
    assert r.status_code == 200


def test_delete_file(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.put("/repos/acme/webapp/contents/a.txt",
               json={"message": "add", "content": _b64("one\n")}, headers=H)
    sha = client.get("/repos/acme/webapp/contents/a.txt", headers=H).json()["sha"]
    r = client.request("DELETE", "/repos/acme/webapp/contents/a.txt",
                       json={"message": "drop", "sha": sha}, headers=H)
    assert r.status_code == 200
    assert r.json()["content"] is None
    assert client.get("/repos/acme/webapp/contents/a.txt", headers=H).status_code == 404


def test_directory_listing_returns_an_array(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    for path in ("README.md", "src/app.py", "src/util/io.py"):
        client.put(f"/repos/acme/webapp/contents/{path}",
                   json={"message": "add", "content": _b64("x\n")}, headers=H)
    root = client.get("/repos/acme/webapp/contents/", headers=H).json()
    assert {(e["path"], e["type"]) for e in root} == {("README.md", "file"), ("src", "dir")}
    src = client.get("/repos/acme/webapp/contents/src", headers=H).json()
    assert {(e["path"], e["type"]) for e in src} == {("src/app.py", "file"), ("src/util", "dir")}


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
    main_sha = client.get(
        "/repos/acme/webapp/branches", headers=H
    ).json()[0]["commit"]["sha"]
    r = client.post(
        "/repos/acme/webapp/git/refs",
        json={"ref": "refs/heads/main", "sha": main_sha},
        headers=H,
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["code"] == "already_exists"


def test_get_branch_and_ref(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    branch = client.get("/repos/acme/webapp/branches/main", headers=H)
    assert branch.status_code == 200
    sha = branch.json()["commit"]["sha"]
    ref = client.get("/repos/acme/webapp/git/ref/heads/main", headers=H)
    assert ref.status_code == 200
    assert ref.json() == {
        "ref": "refs/heads/main",
        "node_id": ref.json()["node_id"],
        "url": ref.json()["url"],
        "object": {"type": "commit", "sha": sha, "url": ref.json()["object"]["url"]},
    }
    assert client.get("/repos/acme/webapp/branches/nope", headers=H).status_code == 404
    assert client.get("/repos/acme/webapp/git/ref/heads/nope", headers=H).status_code == 404


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
