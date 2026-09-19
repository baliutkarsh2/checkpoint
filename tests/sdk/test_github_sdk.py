"""GitHub twin driven by PyGithub, the most widely used Python GitHub SDK."""
from __future__ import annotations

import pytest

github = pytest.importorskip("github")

TWIN = "github"


@pytest.fixture
def gh(twin):
    return github.Github(auth=github.Auth.Token(twin.token), base_url=twin.url)


def test_authenticated_user(gh):
    assert gh.get_user().login == "default-user"


def test_create_repo_then_issue_lifecycle(gh, twin):
    repo = gh.get_user().create_repo("webapp")
    issue = repo.create_issue(title="Login broken", body="500 on /login")
    assert issue.number == 1
    fetched = repo.get_issue(1)
    assert fetched.title == "Login broken"
    fetched.edit(state="closed")
    assert repo.get_issue(1).state == "closed"
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "issues") in ops


def test_list_issues_on_seeded_repo(gh, twin):
    twin.seed("small-project")
    titles = [i.title for i in gh.get_repo("acme/webapp").get_issues(state="open")]
    assert titles, "small-project seeds open issues in acme/webapp"


def test_missing_repo_raises_unknown_object(gh):
    with pytest.raises(github.UnknownObjectException):
        gh.get_repo("acme/does-not-exist")
