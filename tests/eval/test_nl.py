"""Compiling plain-English criteria: exact translations, and honest refusals.

The refusals are the point. Every "must not compile" case below is a criterion
the old checker answered confidently — and often wrongly — by matching only its
first few words.
"""
from __future__ import annotations

import pytest

from checkpoint.eval.nl import Collection, Schema, compile_criterion

SCHEMA = Schema((
    Collection("github", "issues", ("issue", "issues"),
               frozenset({"id", "number", "title", "state", "repo", "labels", "assignees"})),
    Collection("github", "pulls", ("pull request", "pull requests", "pr", "prs"),
               frozenset({"id", "number", "title", "state", "merged", "repo"})),
    Collection("github", "labels", ("label", "labels"), frozenset({"id", "name", "repo"})),
    Collection("slack", "messages", ("message", "messages"),
               frozenset({"id", "text", "channel", "channel_name", "user"})),
    Collection("slack", "channels", ("channel", "channels"), frozenset({"id", "name"})),
))

AMBIGUOUS = Schema(SCHEMA.collections + (
    Collection("linear", "issues", ("issue", "issues"),
               frozenset({"id", "identifier", "title", "state"})),
))


@pytest.mark.parametrize(("criterion", "assertion"), [
    ('An issue titled "Login broken" exists', 'exists(github.issues[title == "Login broken"])'),
    ('A channel named "incidents" exists', 'exists(slack.channels[name == "incidents"])'),
    ("Exactly 2 issues exist", "count(github.issues) == 2"),
    ("At least one issue exists", "count(github.issues) >= 1"),
    ("At most 3 pull requests exist", "count(github.pulls) <= 3"),
    ("No labels exist", "count(github.labels) == 0"),
    ("Exactly 2 issues were created", "count(created.github.issues) == 2"),
    ("At least one message was created", "count(created.slack.messages) >= 1"),
    ("No issues were deleted", "count(deleted.github.issues) == 0"),
    ("No new labels were created", "count(created.github.labels) == 0"),
    ("Exactly 1 issue was modified", "count(changed.github.issues) == 1"),
    ("Issue #2 is closed", 'github.issues[number == 2].state == "closed"'),
    ("Issue #1 still exists", "exists(github.issues[number == 1])"),
    ('The final answer mentions "issue"', 'answer contains "issue"'),
    (r"The answer matches /#\d+/", r"answer ~ /#\d+/"),
    ("The agent made at most 5 calls", "count(trace) <= 5"),
    ("No failed calls", "count(trace[ok == false]) == 0"),
    ("The agent never called DELETE", 'count(trace[op == "delete"]) == 0'),
    ("The agent did not call DELETE `/repos/acme/webapp/issues/1`",
     'count(trace[method == "DELETE" && path == "/repos/acme/webapp/issues/1"]) == 0'),
    ("The agent made no calls to hosts outside the sandbox", "count(egress) == 0"),
    ("Exactly 2 issues exist.", "count(github.issues) == 2"),
])
def test_compiles(criterion, assertion):
    compiled = compile_criterion(criterion, SCHEMA)
    assert compiled is not None, f"{criterion!r} did not compile"
    assert compiled.assertion == assertion
    assert compiled.source == "pattern"


@pytest.mark.parametrize("criterion", [
    # Qualifiers the patterns do not cover: scored as the unqualified claim before.
    "At least 1 issue is assigned to user-alice",
    'An issue titled "Login broken" exists and is closed',
    "At least 2 issues exist in acme/api",
    "All closed issues have a new comment",
    "No issue is open",
    "Issue #1 is closed and labelled bug",
    "At least 1 refund exists for pi_succ",
    'No issue titled "Add dark mode" was closed',
    # Judgement, not state.
    "The agent handled the refund politely",
    "The agent explained why it could not proceed",
    # Unknown nouns.
    "Exactly 2 widgets exist",
])
def test_refuses_rather_than_guesses(criterion):
    assert compile_criterion(criterion, SCHEMA) is None


def test_ambiguous_noun_is_not_guessed():
    # With GitHub and Linear in the same run, "issue" must not silently mean GitHub.
    assert compile_criterion("Exactly 2 issues exist", AMBIGUOUS) is None


def test_qualified_noun_resolves():
    compiled = compile_criterion("Exactly 2 linear issues exist", AMBIGUOUS)
    assert compiled is not None and compiled.assertion == "count(linear.issues) == 2"


def test_title_pattern_requires_the_field():
    # A collection without a title field must not compile a title assertion.
    schema = Schema((Collection("slack", "channels", ("channel",), frozenset({"id", "name"})),))
    assert compile_criterion('A channel titled "x" exists', schema) is None


def test_schema_from_views_reads_nouns_and_fields():
    views = {"github": {"issues": {"key": "id", "tombstone": None, "nouns": ["issue", "issues"],
                                   "items": [{"id": 1, "title": "x"}]}}}
    schema = Schema.from_views(views)
    coll = schema.resolve("issues")
    assert coll is not None and coll.path == "github.issues"
    assert coll.fields == frozenset({"id", "title"})


def test_scenario_comments_are_not_criteria():
    from checkpoint.scenario import parse

    scenario = parse(
        "# s\n## Task\ndo\n## Criteria\n"
        "- [D] Exactly 1 issue was created\n"
        "<!--\n  Authors annotate scenarios; this is a note, not a check:\n"
        "  - [D] At least one issue exists\n-->\n"
        "- [P] The answer is clear\n"
    )
    assert [c.text for c in scenario.criteria] == [
        "Exactly 1 issue was created", "The answer is clear"]
    assert scenario.problems == []
