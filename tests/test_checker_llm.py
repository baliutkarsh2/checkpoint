"""Phase 5 / Plan 05-02: Stage-2 schema-validated [D] LLM-JSON parser tests.

All tests use a fake OpenAI client — no live API. The fake responds to the
single ``chat.completions.create`` call with a pre-canned string.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from checkpoint.checker_llm import (
    Assertion,
    ParseOutcome,
    evaluate,
    parse_assertion,
    try_stage2,
)


# ---------------------------------------------------------------------------
# Fake OpenAI client
# ---------------------------------------------------------------------------

@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Resp:
    choices: list


class _FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        return _Resp(choices=[_Choice(message=_Msg(content=self.content))])


class _FakeChat:
    def __init__(self, content: str):
        self.completions = _FakeCompletions(content)


class FakeClient:
    def __init__(self, content: str):
        self.chat = _FakeChat(content)


def factory(content: str):
    """Return a zero-arg callable that produces a fresh FakeClient."""
    def _f():
        return FakeClient(content)
    return _f


# ---------------------------------------------------------------------------
# parse_assertion: happy paths
# ---------------------------------------------------------------------------

def test_parse_valid_count_eq():
    raw = json.dumps({
        "resource": "refunds",
        "selector": None,
        "operator": "count_eq",
        "value": 2,
    })
    out = parse_assertion("Exactly 2 refunds exist", _client_factory=factory(raw))
    assert isinstance(out.assertion, Assertion)
    assert out.assertion.operator == "count_eq"
    assert out.assertion.value == 2


def test_parse_valid_all_have():
    raw = json.dumps({
        "resource": "issues",
        "selector": {"state": "closed"},
        "operator": "all_have",
        "value": "comment",
    })
    out = parse_assertion("All closed issues have a comment", _client_factory=factory(raw))
    assert isinstance(out.assertion, Assertion)
    assert out.assertion.selector == {"state": "closed"}
    assert out.assertion.value == "comment"


def test_parse_valid_exists_with_title():
    raw = json.dumps({
        "resource": "issue",
        "selector": {"title": "Launch coordination"},
        "operator": "exists",
        "value": None,
    })
    out = parse_assertion('An issue titled "Launch coordination" exists',
                          _client_factory=factory(raw))
    assert out.assertion.operator == "exists"


# ---------------------------------------------------------------------------
# parse_assertion: fall-through reasons
# ---------------------------------------------------------------------------

def test_parse_chatty_prose_falls_through():
    raw = "Sure! Here's the answer: this criterion is about issues being closed."
    out = parse_assertion("Exactly 2 issues are closed", _client_factory=factory(raw))
    assert out.assertion is None
    assert "not valid JSON" in out.reason


def test_parse_markdown_fence_falls_through():
    raw = '```json\n{"resource":"issues","operator":"count_eq","value":2}\n```'
    out = parse_assertion("Exactly 2 issues are closed", _client_factory=factory(raw))
    assert out.assertion is None
    assert "not valid JSON" in out.reason


def test_parse_missing_required_field_falls_through():
    raw = json.dumps({"resource": "issues"})  # operator missing
    out = parse_assertion("anything", _client_factory=factory(raw))
    assert out.assertion is None
    assert "schema validation failed" in out.reason


def test_parse_extra_keys_falls_through():
    raw = json.dumps({
        "resource": "issues",
        "operator": "count_eq",
        "value": 2,
        "explanation": "I think...",  # extra key — strict schema rejects
    })
    out = parse_assertion("anything", _client_factory=factory(raw))
    assert out.assertion is None


def test_parse_bad_operator_falls_through():
    raw = json.dumps({
        "resource": "issues",
        "operator": "magic_pass",
        "value": 1,
    })
    out = parse_assertion("anything", _client_factory=factory(raw))
    assert out.assertion is None


def test_parse_array_not_object():
    raw = json.dumps([{"resource": "issues"}])
    out = parse_assertion("anything", _client_factory=factory(raw))
    assert out.assertion is None


def test_parse_empty_response():
    out = parse_assertion("anything", _client_factory=factory(""))
    assert out.assertion is None
    assert "empty" in out.reason


# ---------------------------------------------------------------------------
# evaluate: programmatic verdict against synthetic state
# ---------------------------------------------------------------------------

def gh(issues=None, labels=None):
    return {
        "issues": {str(i.get("id", n)): i for n, i in enumerate(issues or [])},
        "labels": {str(l.get("name", n)): l for n, l in enumerate(labels or [])},
        "repos": {}, "pulls": {}, "workflow_runs": {}, "comments": {},
    }


def test_evaluate_count_eq_pass():
    a = Assertion(resource="issues", operator="count_eq", value=2)
    s = gh(issues=[{"id": 1, "state": "open"}, {"id": 2, "state": "open"}])
    r = evaluate(a, s)
    assert r.handled and r.passed


def test_evaluate_count_gte():
    a = Assertion(resource="issues", operator="count_gte", value=2)
    s = gh(issues=[{"id": i, "state": "open"} for i in range(3)])
    assert evaluate(a, s).passed


def test_evaluate_count_lte_fail():
    a = Assertion(resource="issues", operator="count_lte", value=1)
    s = gh(issues=[{"id": i} for i in range(3)])
    assert not evaluate(a, s).passed


def test_evaluate_exists_with_title():
    a = Assertion(resource="issues", operator="exists",
                  selector={"title": "Launch"})
    s = gh(issues=[{"id": 1, "title": "Launch", "state": "open"}])
    assert evaluate(a, s).passed


def test_evaluate_not_exists():
    a = Assertion(resource="issues", operator="not_exists", selector={"label": "wip"})
    s = gh(issues=[{"id": 1, "labels": [{"name": "bug"}]}])
    assert evaluate(a, s).passed


def test_evaluate_all_have_comment():
    a = Assertion(resource="issues", operator="all_have",
                  selector={"state": "closed"}, value="comment")
    s = gh(issues=[
        {"id": 1, "state": "closed", "comments": 1},
        {"id": 2, "state": "closed", "comments": 2},
    ])
    assert evaluate(a, s).passed


def test_evaluate_all_have_comment_fail():
    a = Assertion(resource="issues", operator="all_have",
                  selector={"state": "closed"}, value="comment")
    s = gh(issues=[
        {"id": 1, "state": "closed", "comments": 0},
        {"id": 2, "state": "closed", "comments": 1},
    ])
    assert not evaluate(a, s).passed


def test_evaluate_selector_filters_by_state():
    a = Assertion(resource="issues", operator="count_eq",
                  selector={"state": "closed"}, value=2)
    s = gh(issues=[
        {"id": 1, "state": "closed"},
        {"id": 2, "state": "closed"},
        {"id": 3, "state": "open"},
    ])
    assert evaluate(a, s).passed


def test_evaluate_unknown_resource_unhandled():
    a = Assertion(resource="zorgs", operator="count_eq", value=0)
    r = evaluate(a, gh())
    assert not r.handled


# ---------------------------------------------------------------------------
# try_stage2 end-to-end
# ---------------------------------------------------------------------------

def test_try_stage2_happy_path():
    raw = json.dumps({
        "resource": "issues",
        "selector": {"state": "closed"},
        "operator": "count_eq",
        "value": 2,
    })
    s = gh(issues=[
        {"id": 1, "state": "closed"},
        {"id": 2, "state": "closed"},
        {"id": 3, "state": "open"},
    ])
    res, reason = try_stage2("Exactly 2 issues are closed", s,
                             _client_factory=factory(raw))
    assert reason == "ok"
    assert res is not None and res.handled and res.passed


def test_try_stage2_chatty_returns_none():
    raw = "I think the answer is 2."
    res, reason = try_stage2("Exactly 2 issues are closed", gh(),
                             _client_factory=factory(raw))
    assert res is None
    assert "not valid JSON" in reason


def test_try_stage2_schema_invalid_returns_none():
    raw = json.dumps({"resource": "issues", "operator": "invalid_op"})
    res, reason = try_stage2("anything", gh(), _client_factory=factory(raw))
    assert res is None
    assert "schema" in reason.lower()


def test_try_stage2_unknown_noun_falls_through():
    raw = json.dumps({
        "resource": "gizmos",
        "operator": "count_eq",
        "value": 0,
    })
    res, reason = try_stage2("No gizmos exist", gh(),
                             _client_factory=factory(raw))
    assert res is None
    assert "Unknown resource" in reason
