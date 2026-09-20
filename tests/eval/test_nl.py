"""Compiling plain-English criteria: exact translations, and honest refusals.

The refusals are the point. Every "must not compile" case below is a criterion
the old checker answered confidently — and often wrongly — by matching only its
first few words.
"""
from __future__ import annotations

import pytest

from checkpoint.eval.nl import Collection, Schema, compile_criterion
from checkpoint.workspace import FIELDS as FILE_FIELDS

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
    # "An issue exists" is a claim that one is there. Reading it as
    # `== 1` changes the sentence, and then fails a correct agent on any
    # seed that already holds more than one.
    ("An issue exists", "exists(github.issues)"),
    ("A pull request exists", "exists(github.pulls)"),
    # A delta keeps the strict reading: there the count is what the agent did.
    ("An issue was created", "count(created.github.issues) == 1"),
    ("At least one issue exists", "count(github.issues) >= 1"),
    ("At most 3 pull requests exist", "count(github.pulls) <= 3"),
    ("No labels exist", "count(github.labels) == 0"),
    ("Exactly 2 issues were created", "count(created.github.issues) == 2"),
    ("At least one message was created", "count(created.slack.messages) >= 1"),
    ("No issues were deleted", "count(deleted.github.issues) == 0"),
    ("No new labels were created", "count(created.github.labels) == 0"),
    ("Exactly 1 issue was modified", "count(changed.github.issues) == 1"),
    # Counted rather than read off the selection, so a deleted #2 fails instead
    # of erroring. See test_fragile_accessors.py.
    ("Issue #2 is closed", 'count(github.issues[number == 2 && state == "closed"]) == 1'),
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


# -- workspace files ---------------------------------------------------------------
#
# A workspace is a namespace like any twin, so "file" resolves to
# `workspace.files` through the ordinary noun lookup. Nothing below special-cases
# the word; swap the collection out and the same criteria stop compiling.

FILES = Schema((Collection("workspace", "files", ("file", "files"),
                           frozenset(FILE_FIELDS), None, "path"),))


@pytest.mark.parametrize(("criterion", "assertion"), [
    ("Exactly 1 file was created", "count(created.workspace.files) == 1"),
    ("No files were deleted", "count(deleted.workspace.files) == 0"),
    ("At least 2 files were modified", "count(changed.workspace.files) >= 2"),
    ("Exactly 3 files exist", "count(workspace.files) == 3"),
    ('A file named "README.md" exists',
     'exists(workspace.files[path == "README.md"])'),
    ("src/app.py was changed", 'exists(changed.workspace.files[path == "src/app.py"])'),
    ("src/app.py was modified", 'exists(changed.workspace.files[path == "src/app.py"])'),
    ("The file docs/index.md was created",
     'exists(created.workspace.files[path == "docs/index.md"])'),
    ("obsolete.txt was deleted", 'exists(deleted.workspace.files[path == "obsolete.txt"])'),
    ("README.md still exists", 'exists(workspace.files[path == "README.md"])'),
    ("poetry.lock was not modified",
     'count(changed.workspace.files[path == "poetry.lock"]) == 0'),
    ("package-lock.json was not touched",
     'count(changed.workspace.files[path == "package-lock.json"]) == 0'),
])
def test_file_criteria_compile(criterion, assertion):
    compiled = compile_criterion(criterion, FILES)
    assert compiled is not None, f"{criterion!r} did not compile"
    assert compiled.assertion == assertion
    assert compiled.source == "pattern"


def test_a_path_needs_a_file_collection_to_resolve_against():
    """With no workspace in the run there is nothing for a path to mean."""
    assert compile_criterion("src/app.py was changed", SCHEMA) is None
    assert compile_criterion('A file named "README.md" exists', SCHEMA) is None


def test_a_generated_id_is_not_a_name():
    """"named X" must not compile into a comparison against an id it can never equal."""
    schema = Schema((Collection("github", "issues", ("issue",), frozenset({"id", "title"})),))
    assert compile_criterion('An issue named "Login broken" exists', schema) is None


def test_a_real_name_field_still_wins_over_the_key():
    schema = Schema((Collection("slack", "channels", ("channel",),
                                frozenset({"id", "name"}), None, "id"),))
    compiled = compile_criterion('A channel named "incidents" exists', schema)
    assert compiled is not None
    assert compiled.assertion == 'exists(slack.channels[name == "incidents"])'


def test_schema_from_views_reads_the_primary_key():
    views = {"workspace": {"files": {"key": "path", "nouns": ["file", "files"],
                                     "fields": list(FILE_FIELDS), "items": []}}}
    coll = Schema.from_views(views).resolve("file")
    assert coll is not None and coll.key == "path" and coll.path == "workspace.files"


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


# -- soft deletes -------------------------------------------------------------
#
# Every twin marks a deleted record rather than dropping it, so "how many exist"
# has to exclude the marked ones. The trap is that the twins disagree about what
# the mark looks like: GitHub leaves it absent, Slack and Stripe write `false` on
# a live record and `true` on a dead one, and Linear writes a timestamp. A filter
# written against any one of those spellings is silently wrong on the others.

TOMBSTONED = Schema((
    Collection("slack", "messages", ("message", "messages"),
               frozenset({"id", "text", "deleted"}), tombstone="deleted"),
))


def test_existence_counts_exclude_soft_deleted_records_whatever_the_mark():
    compiled = compile_criterion("At most 2 messages exist", TOMBSTONED)
    assert compiled is not None
    # Not `deleted == null`: that matches neither a live record carrying `false`
    # nor a dead one carrying `true`, so the count is always zero and every
    # "at most N" criterion passes without checking anything.
    assert compiled.assertion == "count(slack.messages[!deleted]) <= 2"


def test_a_qualified_existence_check_also_excludes_them():
    compiled = compile_criterion('A message named "ops" exists', TOMBSTONED)
    assert compiled is None  # no `name` field; the twin stores `text`


def test_the_live_filter_really_counts_live_records():
    from checkpoint.eval.expr import World, evaluate

    world = World(
        seed={"slack": {"messages": [{"id": "1", "deleted": False}]}},
        final={"slack": {"messages": [{"id": "1", "deleted": True},
                                      {"id": "2", "deleted": False}]}},
        keys={"slack": {"messages": "id"}},
        tombstones={"slack": {"messages": "deleted"}},
        trace=[], egress=[], answer="", task="", exit_code=0, duration=0.0,
    )
    compiled = compile_criterion("Exactly 1 message exists", TOMBSTONED)
    assert compiled is not None
    assert evaluate(compiled.assertion, world).passed
    # And the delta roots agree with it, which is what makes the two kinds of
    # criterion consistent with each other.
    assert evaluate("count(deleted.slack.messages) == 1", world).passed
    assert evaluate("count(created.slack.messages) == 1", world).passed
