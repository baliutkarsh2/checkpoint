"""The assertion language: what passes, what fails, and what must be an error.

The error cases matter most. Every one of them used to be scored as a quiet
pass or fail by the old checker, which is how a gate returns the wrong verdict.
"""
from __future__ import annotations

import pytest

from checkpoint.eval.expr import (
    ExprSyntaxError,
    World,
    diff_collection,
    evaluate,
    parse,
    referenced_twins,
    render,
)

WORLD = World(
    seed={
        "github": {
            "issues": [
                {"id": 1, "number": 1, "repo": "acme/webapp", "title": "Old bug",
                 "state": "open", "labels": [{"name": "bug"}]},
                {"id": 2, "number": 2, "repo": "acme/api", "title": "Docs",
                 "state": "closed", "labels": []},
            ],
            "repos": [{"id": 10, "full_name": "acme/webapp"}],
        },
        "linear": {"issues": [{"id": "L1", "identifier": "ENG-10", "archivedAt": None,
                               "assigneeId": "user-bob"}]},
    },
    final={
        "github": {
            "issues": [
                {"id": 1, "number": 1, "repo": "acme/webapp", "title": "Old bug",
                 "state": "closed", "labels": [{"name": "bug"}]},
                {"id": 2, "number": 2, "repo": "acme/api", "title": "Docs",
                 "state": "closed", "labels": []},
                {"id": 3, "number": 3, "repo": "acme/webapp", "title": "Login broken",
                 "state": "open", "labels": [{"name": "bug"}, {"name": "p0"}]},
            ],
            "repos": [{"id": 10, "full_name": "acme/webapp"}],
        },
        "linear": {"issues": [{"id": "L1", "identifier": "ENG-10", "archivedAt": "2026-09-19",
                               "assigneeId": "user-alice"}]},
    },
    keys={"linear": {"issues": "id"}},
    tombstones={"linear": {"issues": "archivedAt"}},
    trace=[
        {"twin": "github", "method": "POST", "path": "/repos/acme/webapp/issues",
         "status": 201, "op": "create", "resource": "issues", "ok": True},
        {"twin": "github", "method": "PATCH", "path": "/repos/acme/webapp/issues/1",
         "status": 200, "op": "update", "resource": "issues", "ok": True},
        {"twin": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "status": 200, "op": "create", "resource": "messages", "ok": False},
    ],
    egress=[{"host": "api.tavily.com", "allowed": False}],
    answer="Opened issue #3 and closed #1",
    exit_code=0,
    duration=12.5,
)


@pytest.mark.parametrize(("assertion", "status"), [
    # state
    ('exists(github.issues[title == "Login broken"])', "pass"),
    ('exists(github.issues[title == "login broken"])', "fail"),
    ('exists(github.issues[lower(title) == "login broken"])', "pass"),
    ('count(github.issues) == 3', "pass"),
    ('count(github.issues[state == "open"]) == 1', "pass"),
    ('github.issues[number == 3].state == "open"', "pass"),
    ('github.issues[number == 1].state == "open"', "fail"),
    ('"p0" in github.issues[number == 3].labels[*].name', "pass"),
    ('"wontfix" in github.issues[number == 3].labels[*].name', "fail"),
    ('all(github.issues[repo == "acme/api"], state == "closed")', "pass"),
    ('any(github.issues, title ~ /login/i)', "pass"),
    # what the agent changed
    ('count(created.github.issues) == 1', "pass"),
    ('count(created.github.issues) == 2', "fail"),
    ('count(deleted.github.issues) == 0', "pass"),
    ('count(changed.github.issues) == 1', "pass"),
    ('count(seed.github.issues) == 2', "pass"),
    # a soft delete is a delete
    ('count(deleted.linear.issues) == 1', "pass"),
    ('count(created.linear.issues) == 0', "pass"),
    ('linear.issues[identifier == "ENG-10"].assigneeId == "user-alice"', "pass"),
    # calls
    ('count(trace[method == "DELETE"]) == 0', "pass"),
    ('count(trace[op == "create"]) == 2', "pass"),
    ('count(trace[ok == false]) == 1', "pass"),
    ('count(trace[twin == "github"]) == 2', "pass"),
    ('count(trace[path ~ /issues$/]) == 1', "pass"),
    # answer and process facts
    (r'answer ~ /issue #\d+/', "pass"),
    ('answer contains "closed #1"', "pass"),
    ('duration < 30', "pass"),
    ('exit_code == 0', "pass"),
    ('count(egress[allowed == false]) == 0', "fail"),
    # boolean combinations
    ('count(created.github.issues) >= 1 && count(deleted.github.issues) == 0', "pass"),
    ('!exists(github.issues[title == "nope"])', "pass"),
    ('not exists(github.issues[title == "Login broken"])', "fail"),
    ('exists(github.issues[title == "nope"]) || exit_code == 0', "pass"),
])
def test_assertions(assertion, status):
    assert evaluate(assertion, WORLD).status == status


@pytest.mark.parametrize(("assertion", "message"), [
    ('exists(github.isues[title == "x"])', "no collection 'isues'"),
    ('exists(github.issues[titel == "x"])', "no field 'titel'"),
    ('exists(gitlab.issues)', "unknown name 'gitlab'"),
    ('count(github.issues)', "must be true/false"),
    ('github.issues[number == 99].state == "open"', "no items"),
    ('github.issues[repo == "acme/webapp"].state == "open"', "2 items"),
    ('count(github.issues) == "three"', "cannot compare"),
    ('exists(github.issues[title])', "true/false condition"),
    ('count(github.issues) >', "syntax"),
    ('exists(github)', "needs a collection"),
    ('github.issues[number == 1].labels.name', "use [*].name"),
    ('countt(github.issues) == 1', "unknown function"),
])
def test_mistakes_are_errors_not_verdicts(assertion, message):
    outcome = evaluate(assertion, WORLD)
    assert outcome.status == "error", outcome
    assert message in outcome.detail


def test_failure_detail_shows_the_values_that_decided_it():
    outcome = evaluate("count(created.github.issues) == 2", WORLD)
    assert outcome.detail == "count(created.github.issues) = 1, expected == 2"


def test_exists_detail_reports_matches():
    assert "matched 0 items" in evaluate('exists(github.issues[title == "nope"])', WORLD).detail


def test_diff_handles_hard_and_soft_deletes():
    seed = [{"id": 1, "archived": None}, {"id": 2, "archived": None}]
    final = [{"id": 2, "archived": "2026-01-01"}, {"id": 3, "archived": None}]
    delta = diff_collection(seed, final, key_field="id", tombstone="archived")
    assert [i["id"] for i in delta["created"]] == [3]
    assert sorted(i["id"] for i in delta["deleted"]) == [1, 2]
    assert delta["changed"] == []


def test_render_round_trips():
    for text in ('count(created.github.issues) == 1',
                 'exists(github.issues[title == "a b" && number >= 2])',
                 '"x" in github.issues[*].title'):
        assert render(parse(text)) == text


def test_referenced_twins():
    assert referenced_twins('count(created.github.issues) == 1') == {"github"}
    assert referenced_twins('exists(slack.messages[text ~ /x/]) && count(github.issues) == 0') == {
        "github", "slack"}
    assert referenced_twins('count(trace) < 5 && answer contains "x"') == set()


def test_parse_rejects_empty():
    with pytest.raises(ExprSyntaxError):
        parse("   ")
