"""GitHub twin driven by PyGithub, the most widely used Python GitHub SDK.

Every test here is an operation agents actually perform — open an issue, comment
on it, push a file to a branch, open and merge a pull request — driven through
the SDK's own objects and transport. A failure means a *correct* agent would
fail against the twin.
"""
from __future__ import annotations

import time

import pytest

github = pytest.importorskip("github")

TWIN = "github"


@pytest.fixture
def gh(twin):
    """A PyGithub client with retries and pacing off, so tests fail fast."""
    return github.Github(auth=github.Auth.Token(twin.token), base_url=twin.url,
                         seconds_between_requests=0, seconds_between_writes=0, retry=None)


def _repo_with_branch(gh, twin, *, branch: str = "feature"):
    """A repo whose ``branch`` is one commit ahead of main — the shape a PR needs."""
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    repo.create_git_ref(f"refs/heads/{branch}", repo.get_branch("main").commit.sha)
    repo.create_file("src/feature.js", "feat: add widget", "export const widget = 1;\n",
                     branch=branch)
    return repo


# --- identity and discovery ----------------------------------------------

def test_authenticated_user(gh):
    assert gh.get_user().login == "default-user"


def test_organization_repositories(gh, twin):
    twin.seed("small-project")
    names = [r.full_name for r in gh.get_organization("acme").get_repos()]
    assert set(names) == {"acme/webapp", "acme/api"}


def test_rate_limit_endpoint(gh):
    overview = gh.get_rate_limit()
    assert overview.rate.limit > 0 and overview.rate.remaining <= overview.rate.limit
    assert overview.resources.core.reset.timestamp() > time.time() - 60


# --- issues ---------------------------------------------------------------

def test_create_repo_then_issue_lifecycle(gh, twin):
    repo = gh.get_user().create_repo("webapp", auto_init=True)
    issue = repo.create_issue(title="Login broken", body="500 on /login",
                              labels=["bug"], assignees=["alice"])
    assert issue.number == 1
    assert [label.name for label in issue.labels] == ["bug"]
    assert [user.login for user in issue.assignees] == ["alice"]

    fetched = repo.get_issue(1)
    assert fetched.title == "Login broken"
    fetched.edit(title="Login broken on Safari", state="closed")
    reread = repo.get_issue(1)
    assert reread.state == "closed" and reread.title.endswith("Safari")

    stored = twin.state()["issues"]["default-user/webapp#1"]
    assert stored["state"] == "closed"
    assert [a["login"] for a in stored["assignees"]] == ["alice"]
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "issues") in ops and ("update", "issues") in ops


def test_issue_object_from_create_is_usable(gh, twin):
    """PyGithub follows ``issue.url``; without it every follow-up call breaks."""
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    issue = repo.create_issue(title="Flaky test")
    comment = issue.create_comment("Seen twice today")
    comment.edit("Seen three times today")
    bodies = [c.body for c in issue.get_comments()]
    assert bodies == ["Seen three times today"]
    assert repo.get_issue(issue.number).comments == 1
    comment.delete()
    assert repo.get_issue(issue.number).comments == 0


def test_labels_on_issue_and_repository(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    issue = repo.get_issue(1)
    issue.add_to_labels("needs-triage")
    assert "needs-triage" in {label.name for label in repo.get_issue(1).labels}
    # Labelling an issue creates the repository label, as GitHub does.
    assert "needs-triage" in {label.name for label in repo.get_labels()}
    issue.remove_from_labels("needs-triage")
    assert "needs-triage" not in {label.name for label in repo.get_issue(1).labels}

    created = repo.create_label("p1", "ff0000")
    assert created.color == "ff0000"
    with pytest.raises(github.GithubException) as excinfo:
        repo.create_label("p1", "ff0000")
    assert excinfo.value.status == 422
    repo.get_label("p1").delete()
    assert "p1" not in {label.name for label in repo.get_labels()}


def test_assignees_added_and_removed(gh, twin):
    twin.seed("small-project")
    issue = gh.get_repo("acme/webapp").get_issue(1)
    issue.add_to_assignees("alice", "bob")
    assert {u.login for u in issue.assignees} == {"alice", "bob"}
    issue.remove_from_assignees("bob")
    assert {u.login for u in issue.assignees} == {"alice"}


def test_list_issues_filters_and_paginates(gh, twin):
    twin.seed("large-backlog")
    repo = gh.get_repo("acme/webapp")
    open_issues = list(repo.get_issues(state="open"))
    assert len(open_issues) == 15, "PyGithub must follow the Link header past page 1"
    bugs = list(repo.get_issues(state="open", labels=["bug"]))
    assert bugs and all("bug" in {label.name for label in i.labels} for i in bugs)
    repo.get_issue(1).edit(state="closed")
    assert len(list(repo.get_issues(state="open"))) == 14
    assert len(list(repo.get_issues(state="all"))) == 15


def test_comments_paginate(gh, twin):
    """``per_page`` and the Link header have to work, or long threads truncate."""
    twin.seed("small-project")
    issue = gh.get_repo("acme/webapp").get_issue(1)
    for index in range(7):
        issue.create_comment(f"comment {index}")
    paged = github.Github(auth=github.Auth.Token(twin.token), base_url=twin.url, per_page=2,
                          seconds_between_requests=0, retry=None)
    bodies = [c.body for c in paged.get_repo("acme/webapp").get_issue(1).get_comments()]
    assert bodies == [f"comment {index}" for index in range(7)]


def test_search_issues_with_qualifiers(gh, twin):
    twin.seed("small-project")
    titles = [i.title for i in gh.search_issues("dark mode", repo="acme/webapp", state="open")]
    assert titles == ["Add dark mode"]
    assert [i.title for i in gh.search_issues("", repo="acme/webapp", label="bug")] == [
        "Login broken on Safari"]
    assert list(gh.search_issues("dark mode", repo="acme/webapp", state="closed")) == []


# --- files, branches and commits -----------------------------------------

def test_file_lifecycle_on_a_branch(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    main_sha = repo.get_branch("main").commit.sha
    repo.create_git_ref("refs/heads/docs", main_sha)

    created = repo.create_file("docs/guide.md", "docs: add guide", "# Guide\n", branch="docs")
    assert repo.get_contents("docs/guide.md", ref="docs").decoded_content == b"# Guide\n"
    with pytest.raises(github.UnknownObjectException):
        repo.get_contents("docs/guide.md", ref="main")  # the commit landed on `docs` only

    repo.update_file("docs/guide.md", "docs: expand guide", "# Guide\n\nMore.\n",
                     created["content"].sha, branch="docs")
    with pytest.raises(github.GithubException) as excinfo:
        repo.update_file("docs/guide.md", "docs: stale", "nope", created["content"].sha,
                         branch="docs")
    assert excinfo.value.status == 409, "a stale blob sha must conflict, not overwrite"

    current = repo.get_contents("docs/guide.md", ref="docs")
    repo.delete_file("docs/guide.md", "docs: drop guide", current.sha, branch="docs")
    with pytest.raises(github.UnknownObjectException):
        repo.get_contents("docs/guide.md", ref="docs")


def test_directory_listing_and_readme(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    root = repo.get_contents("")
    assert {entry.path for entry in root} == {"README.md", "src"}
    assert {entry.type for entry in root} == {"file", "dir"}
    assert repo.get_readme().decoded_content.startswith(b"# webapp")


def test_branches_and_refs(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    main_sha = repo.get_branch("main").commit.sha
    ref = repo.create_git_ref("refs/heads/release", main_sha)
    assert ref.object.sha == main_sha
    assert repo.get_git_ref("heads/release").object.sha == main_sha
    assert {b.name for b in repo.get_branches()} == {"main", "fix-login-bug", "release"}
    assert [r.ref for r in repo.get_git_matching_refs("heads/rel")] == ["refs/heads/release"]
    repo.get_git_ref("heads/release").delete()
    assert "release" not in {b.name for b in repo.get_branches()}
    assert "release" not in twin.state()["repos"]["acme/webapp"]["branches"]


def test_commits_reflect_pushes(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    repo.create_file("CHANGELOG.md", "docs: start changelog", "## 0.1\n")
    messages = [c.commit.message for c in repo.get_commits()]
    assert messages[0] == "docs: start changelog"
    assert repo.get_commits(path="CHANGELOG.md").totalCount == 1


# --- pull requests --------------------------------------------------------

def test_pull_request_lifecycle(gh, twin):
    repo = _repo_with_branch(gh, twin)
    pull = repo.create_pull(title="Add widget", body="Closes #1", head="feature", base="main")
    assert pull.number == 3, "issues and pull requests share one number space"
    assert [f.filename for f in pull.get_files()] == ["src/feature.js"]
    assert pull.mergeable is True

    pull.create_issue_comment("Ready for review")
    pull.add_to_labels("enhancement")
    pull.create_review_request(reviewers=["reviewer1"])
    refetched = repo.get_pull(pull.number)
    assert [u.login for u in refetched.requested_reviewers] == ["reviewer1"]
    assert "enhancement" in {label.name for label in refetched.labels}

    assert repo.get_pull(pull.number).create_review(body="ok", event="APPROVE").state == "APPROVED"
    result = repo.get_pull(pull.number).merge(merge_method="squash")
    assert result.merged is True

    merged = repo.get_pull(pull.number)
    assert merged.state == "closed" and merged.merged is True
    assert merged.is_merged() is True
    assert [f.filename for f in merged.get_files()] == ["src/feature.js"]
    assert repo.get_contents("src/feature.js").decoded_content == b"export const widget = 1;\n"
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "pulls") in ops and ("update", "pulls") in ops


def test_pull_request_is_also_an_issue(gh, twin):
    repo = _repo_with_branch(gh, twin)
    pull = repo.create_pull(title="Add widget", head="feature", base="main")
    as_issue = repo.get_issue(pull.number)
    assert as_issue.pull_request is not None
    as_issue.create_comment("via the issues endpoint")
    assert [c.body for c in pull.get_issue_comments()] == ["via the issues endpoint"]
    numbers = {i.number for i in repo.get_issues(state="open")}
    assert pull.number in numbers, "GitHub lists pull requests among a repo's issues"
    assert pull.as_issue().title == "Add widget"


def test_pull_requests_listed_and_filtered(gh, twin):
    repo = _repo_with_branch(gh, twin)
    repo.create_pull(title="Add widget", head="feature", base="main")
    assert [p.head.ref for p in repo.get_pulls(state="open")] == ["feature"]
    assert [p.head.ref for p in repo.get_pulls(state="open", base="main")] == ["feature"]
    assert list(repo.get_pulls(state="open", base="develop")) == []
    with pytest.raises(github.GithubException) as excinfo:
        repo.create_pull(title="Duplicate", head="feature", base="main")
    assert excinfo.value.status == 422


def test_conflicting_pull_request_cannot_merge(gh, twin):
    twin.seed("merge-conflict")
    pull = gh.get_repo("acme/webapp").get_pull(1)
    assert pull.mergeable is False
    with pytest.raises(github.GithubException) as excinfo:
        pull.merge()
    assert excinfo.value.status == 405
    assert twin.views()["pulls"]["items"][0]["merged"] is False


def test_review_comment_on_a_pull_request(gh, twin):
    repo = _repo_with_branch(gh, twin)
    pull = repo.create_pull(title="Add widget", head="feature", base="main")
    comment = pull.create_review_comment("nit: rename this", pull.get_commits()[0],
                                         "src/feature.js", line=1)
    assert comment.path == "src/feature.js"
    assert [c.body for c in pull.get_review_comments()] == ["nit: rename this"]


# --- actions and releases -------------------------------------------------

def test_workflows_and_runs(gh, twin):
    twin.seed("ci-cd-pipeline")
    repo = gh.get_repo("acme/webapp")
    assert {w.name for w in repo.get_workflows()} == {"ci", "deploy"}
    runs = repo.get_workflow_runs()
    assert runs.totalCount == 3
    failed = [r for r in repo.get_workflow_runs(status="failure")]
    assert len(failed) == 1 and failed[0].conclusion == "failure"
    assert failed[0].rerun() is True
    assert twin.state()["workflow_runs"]["acme/webapp#101"]["status"] == "queued"
    workflow = next(w for w in repo.get_workflows() if w.name == "deploy")
    assert workflow.create_dispatch("main") is True
    assert len(twin.state()["workflow_runs"]) == 4


def test_create_release(gh, twin):
    twin.seed("small-project")
    repo = gh.get_repo("acme/webapp")
    release = repo.create_git_release("v1.0.0", "v1.0.0", "First cut")
    assert release.tag_name == "v1.0.0"
    assert repo.get_latest_release().tag_name == "v1.0.0"
    assert "v1.0.0" in twin.state()["repos"]["acme/webapp"]["tags"]


# --- errors and fault modes ----------------------------------------------

def test_typed_errors(gh, twin):
    twin.seed("small-project")
    with pytest.raises(github.UnknownObjectException):
        gh.get_repo("acme/does-not-exist")
    with pytest.raises(github.GithubException) as excinfo:
        gh.get_repo("acme/webapp").create_pull(title="x", head="nope", base="main")
    assert excinfo.value.status == 422
    assert excinfo.value.data["errors"][0]["field"] == "head"


def test_bad_credentials_under_strict_auth(twin):
    twin.seed("small-project")
    twin.configure(strict_auth=True)
    client = github.Github(auth=github.Auth.Token("ghp_CHECKPOINTFAKEnotthetwinstoken"),
                           base_url=twin.url, retry=None)
    with pytest.raises(github.BadCredentialsException):
        client.get_repo("acme/webapp")


def test_rate_limit_mode_completes_without_hanging(twin):
    """A 429 must be recoverable: PyGithub's default retry waits, then succeeds."""
    twin.seed("small-project")
    twin.configure(rate_limit=2)
    client = github.Github(auth=github.Auth.Token(twin.token), base_url=twin.url,
                           seconds_between_requests=0, seconds_between_writes=0)
    started = time.monotonic()
    for _ in range(4):
        client.get_repo("acme/webapp")
    elapsed = time.monotonic() - started
    assert elapsed < 30, "the twin's rate-limit window must reset while the SDK backs off"
    assert any(entry["status"] == 429 for entry in twin.trace())


def test_read_only_mode_refuses_writes(gh, twin):
    twin.seed("small-project")
    twin.configure(read_only=True)
    repo = gh.get_repo("acme/webapp")
    with pytest.raises(github.GithubException) as excinfo:
        repo.create_issue(title="should not land")
    assert excinfo.value.status == 403
    assert len(twin.state()["issues"]) == 2


# --- views ----------------------------------------------------------------

def test_views_expose_denormalized_records(gh, twin):
    repo = _repo_with_branch(gh, twin)
    repo.create_issue(title="Track rollout", labels=["bug"], assignees=["alice"])
    repo.create_pull(title="Add widget", head="feature", base="main")
    collections = twin.views()
    issue = next(i for i in collections["issues"]["items"] if i["title"] == "Track rollout")
    assert issue["repo"] == "acme/webapp"
    assert issue["labels"] == ["bug"] and issue["assignees"] == ["alice"]
    assert collections["issues"]["key"] == "key"
    assert collections["pulls"]["nouns"] == ["pull request", "pull requests"]
    assert {f["path"] for f in collections["files"]["items"] if f["repo"] == "acme/webapp"} == {
        "README.md", "src/session.js"}
