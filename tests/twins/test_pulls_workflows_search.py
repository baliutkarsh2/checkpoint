"""GitHub twin: pull requests, workflows and search."""
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


def _setup_repo_with_branch(client, branch: str = "feature"):
    """A repo whose ``branch`` is one commit ahead of main — what a PR needs."""
    import base64

    if "acme/webapp" not in gh.STATE["repos"]:
        client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    main_sha = client.get("/repos/acme/webapp/branches", headers=H).json()[0]["commit"]["sha"]
    client.post(
        "/repos/acme/webapp/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": main_sha},
        headers=H,
    )
    client.put(
        f"/repos/acme/webapp/contents/src/{branch}.py",
        json={"message": f"feat: {branch}",
              "content": base64.b64encode(b"x = 1\n").decode(), "branch": branch},
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
    # Hypermedia links SDKs build their next call from.
    assert pr["issue_url"].endswith("/repos/acme/webapp/issues/1")
    assert pr["url"].endswith("/repos/acme/webapp/pulls/1")
    # Private fields stripped.
    assert "_reviews" not in pr

    # List open PRs.
    r = client.get("/repos/acme/webapp/pulls", headers=H)
    assert len(r.json()) == 1

    # Get one.
    r = client.get("/repos/acme/webapp/pulls/1", headers=H)
    assert r.status_code == 200

    # The diff is computed from head vs base.
    r = client.get("/repos/acme/webapp/pulls/1/files", headers=H)
    assert [f["filename"] for f in r.json()] == ["src/feature.py"]

    # Review comment on a changed file.
    r = client.post(
        "/repos/acme/webapp/pulls/1/comments",
        json={"body": "looks good", "path": "src/feature.py", "line": 1},
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

    # State now closed, and the merge landed on the base branch.
    pr = client.get("/repos/acme/webapp/pulls/1", headers=H).json()
    assert pr["state"] == "closed"
    assert pr["merged"] is True
    assert client.get("/repos/acme/webapp/contents/src/feature.py",
                      headers=H).status_code == 200

    # Can't merge twice: GitHub answers 405 for an unmergeable pull request.
    r = client.put(
        "/repos/acme/webapp/pulls/1/merge",
        json={},
        headers=H,
    )
    assert r.status_code == 405


def test_pr_404(client):
    r = client.get("/repos/acme/webapp/pulls/999", headers=H)
    assert r.status_code == 404


def test_list_pulls_filter_by_state_head_base(client):
    _setup_repo_with_branch(client, "feature")
    _setup_repo_with_branch(client, "feature-2")
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "A", "head": "feature", "base": "main"},
        headers=H,
    )
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "B", "head": "feature-2", "base": "main"},
        headers=H,
    )
    # Close PR #2.
    client.patch("/repos/acme/webapp/pulls/2", json={"state": "closed"}, headers=H)
    r = client.get("/repos/acme/webapp/pulls?state=open", headers=H)
    assert len(r.json()) == 1
    r = client.get("/repos/acme/webapp/pulls?state=all", headers=H)
    assert len(r.json()) == 2
    r = client.get("/repos/acme/webapp/pulls?head=feature&state=all", headers=H)
    assert len(r.json()) == 1


def test_second_pull_request_for_the_same_branch_is_rejected(client):
    _setup_repo_with_branch(client)
    body = {"title": "A", "head": "feature", "base": "main"}
    assert client.post("/repos/acme/webapp/pulls", json=body, headers=H).status_code == 201
    r = client.post("/repos/acme/webapp/pulls", json=body, headers=H)
    assert r.status_code == 422
    assert "already exists" in r.json()["errors"][0]["message"]


def test_pull_request_needs_commits_between_the_branches(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    main_sha = client.get("/repos/acme/webapp/branches", headers=H).json()[0]["commit"]["sha"]
    client.post("/repos/acme/webapp/git/refs",
                json={"ref": "refs/heads/idle", "sha": main_sha}, headers=H)
    r = client.post("/repos/acme/webapp/pulls",
                    json={"title": "nothing to do", "head": "idle", "base": "main"}, headers=H)
    assert r.status_code == 422
    assert "No commits between" in r.json()["errors"][0]["message"]
    r = client.post("/repos/acme/webapp/pulls",
                    json={"title": "x", "head": "ghost-branch", "base": "main"}, headers=H)
    assert r.status_code == 422


def test_issue_endpoints_resolve_pull_requests(client):
    """Issues and PRs share one number space; /issues/{n} answers for both."""
    _setup_repo_with_branch(client)
    issue = client.post("/repos/acme/webapp/issues", json={"title": "Bug"}, headers=H).json()
    pr = client.post("/repos/acme/webapp/pulls",
                     json={"title": "Fix", "head": "feature", "base": "main"},
                     headers=H).json()
    assert pr["number"] == issue["number"] + 1

    as_issue = client.get(f"/repos/acme/webapp/issues/{pr['number']}", headers=H)
    assert as_issue.status_code == 200
    assert as_issue.json()["pull_request"]["url"].endswith(f"/pulls/{pr['number']}")

    # Labels and comments applied through the issues endpoints land on the PR.
    client.post(f"/repos/acme/webapp/issues/{pr['number']}/labels",
                json={"labels": ["ready"]}, headers=H)
    client.post(f"/repos/acme/webapp/issues/{pr['number']}/comments",
                json={"body": "ship it"}, headers=H)
    stored_pr = gh.STATE["pulls"][f"acme/webapp#{pr['number']}"]
    assert [lab["name"] for lab in stored_pr["labels"]] == ["ready"]
    assert stored_pr["comments"] == 1
    assert gh.STATE["issues"][f"acme/webapp#{issue['number']}"]["labels"] == []

    # The issue list carries pull requests too, as GitHub's does.
    listed = client.get("/repos/acme/webapp/issues?state=all", headers=H).json()
    assert {i["number"] for i in listed} == {issue["number"], pr["number"]}
    assert [i for i in listed if i["number"] == pr["number"]][0]["pull_request"]


def test_pr_update_branch_endpoint(client):
    _setup_repo_with_branch(client)
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "x", "head": "feature", "base": "main"},
        headers=H,
    )
    before = client.get("/repos/acme/webapp/pulls/1", headers=H).json()["head"]["sha"]
    r = client.put("/repos/acme/webapp/pulls/1/update-branch", headers=H)
    assert r.status_code == 202
    after = client.get("/repos/acme/webapp/pulls/1", headers=H).json()["head"]["sha"]
    assert before != after


def test_pr_diff(client):
    """api.github.com serves the diff by media type, not a `.diff` path."""
    _setup_repo_with_branch(client)
    client.post(
        "/repos/acme/webapp/pulls",
        json={"title": "x", "head": "feature", "base": "main"},
        headers=H,
    )
    r = client.get("/repos/acme/webapp/pulls/1",
                   headers={**H, "Accept": "application/vnd.github.diff"})
    assert r.status_code == 200
    assert r.text.startswith("diff --git a/src/feature.py b/src/feature.py")


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
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    client.post(
        "/repos/acme/webapp/issues",
        json={"title": "auth bug", "body": "Login broken on Safari", "labels": ["bug"]},
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


def test_search_issue_qualifiers(client):
    """Agents type qualifiers; unparsed ones silently return the wrong set."""
    _setup_repo_with_branch(client)
    client.post("/repos/acme/webapp/issues",
                json={"title": "auth bug", "labels": ["bug"]}, headers=H)
    client.post("/repos/acme/webapp/issues", json={"title": "docs typo"}, headers=H)
    client.patch("/repos/acme/webapp/issues/2", json={"state": "closed"}, headers=H)
    client.post("/repos/acme/webapp/pulls",
                json={"title": "auth fix", "head": "feature", "base": "main"}, headers=H)

    def search(q):
        return {i["title"] for i in client.get("/search/issues", params={"q": q},
                                               headers=H).json()["items"]}

    assert search("repo:acme/webapp is:issue is:open") == {"auth bug"}
    assert search("repo:acme/webapp is:pr") == {"auth fix"}
    assert search("auth repo:acme/webapp") == {"auth bug", "auth fix"}
    assert search("repo:acme/webapp label:bug") == {"auth bug"}
    assert search("repo:acme/webapp state:closed") == {"docs typo"}
    assert search("repo:acme/other-repo") == set()


def test_search_pagination_has_a_link_header(client):
    client.post("/user/repos", json={"name": "webapp", "owner": "acme"}, headers=H)
    for index in range(5):
        client.post("/repos/acme/webapp/issues", json={"title": f"issue {index}"}, headers=H)
    r = client.get("/search/issues?q=issue&per_page=2", headers=H)
    assert r.json()["total_count"] == 5
    assert len(r.json()["items"]) == 2
    assert 'rel="next"' in r.headers["Link"]
