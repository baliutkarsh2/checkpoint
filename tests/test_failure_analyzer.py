"""Phase 5 / Plan 05-03: failure analyzer tests.

All paths exercised with a fake OpenAI client (no live API).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from checkpoint.failure_analyzer import analyze


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
    def __init__(self, content: str, raises: Exception | None = None):
        self.content = content
        self.raises = raises
        self.last_kw = None

    def create(self, **kw):
        self.last_kw = kw
        if self.raises:
            raise self.raises
        return _Resp(choices=[_Choice(message=_Msg(content=self.content))])


class FakeClient:
    def __init__(self, content: str = "", raises: Exception | None = None):
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(content, raises)})()


def factory(content: str = "", raises: Exception | None = None):
    def _f():
        return FakeClient(content, raises)
    return _f


# ---------------------------------------------------------------------------

def test_no_failures_returns_empty():
    assert analyze([], task="x", final_answer="", trace=[], state={}) == {}


def test_happy_path_aligns_by_exact_text():
    raw = json.dumps({"analyses": [
        {"criterion": "Exactly 2 issues are closed",
         "why": "Trace entry 5 closed only 1 issue."},
        {"criterion": "All closed issues have a comment",
         "why": "Issue #2 was closed at entry 7 with no follow-up comment."},
    ]})
    out = analyze(
        ["Exactly 2 issues are closed", "All closed issues have a comment"],
        task="close stale", final_answer="done",
        trace=[{"i": i} for i in range(10)], state={"issues": {}},
        _client_factory=factory(raw),
    )
    assert out["Exactly 2 issues are closed"].startswith("Trace entry 5")
    assert "Issue #2" in out["All closed issues have a comment"]


def test_positional_fallback_when_text_mismatch():
    raw = json.dumps({"analyses": [
        {"criterion": "different text", "why": "first paragraph"},
        {"criterion": "different text 2", "why": "second paragraph"},
    ]})
    out = analyze(
        ["A", "B"], task="t", final_answer="", trace=[], state={},
        _client_factory=factory(raw),
    )
    assert out["A"] == "first paragraph"
    assert out["B"] == "second paragraph"


def test_invalid_json_returns_empty():
    out = analyze(
        ["A"], task="t", final_answer="", trace=[], state={},
        _client_factory=factory("Sure! Here you go."),
    )
    assert out == {}


def test_empty_response_returns_empty():
    out = analyze(
        ["A"], task="t", final_answer="", trace=[], state={},
        _client_factory=factory(""),
    )
    assert out == {}


def test_openai_raises_returns_empty():
    out = analyze(
        ["A"], task="t", final_answer="", trace=[], state={},
        _client_factory=factory(raises=RuntimeError("network down")),
    )
    assert out == {}


def test_trace_truncated_to_max_entries():
    raw = json.dumps({"analyses": [{"criterion": "A", "why": "ok"}]})
    big_trace = [{"i": i} for i in range(500)]
    fake = FakeClient(raw)
    out = analyze(
        ["A"], task="t", final_answer="", trace=big_trace, state={},
        _client_factory=lambda: fake,
    )
    assert out["A"] == "ok"
    body = json.loads(fake.chat.completions.last_kw["messages"][1]["content"])
    assert len(body["trace"]) <= 201  # 200 + truncation marker


def test_state_truncated_when_huge():
    raw = json.dumps({"analyses": [{"criterion": "A", "why": "ok"}]})
    huge = {f"key{i}": "x" * 100 for i in range(500)}
    fake = FakeClient(raw)
    out = analyze(
        ["A"], task="t", final_answer="", trace=[], state=huge,
        _client_factory=lambda: fake,
    )
    assert out["A"] == "ok"
    body = json.loads(fake.chat.completions.last_kw["messages"][1]["content"])
    assert body["state"].get("_truncated") is True


def test_partial_analyses_only_one_criterion():
    raw = json.dumps({"analyses": [
        {"criterion": "A", "why": "explanation A"},
    ]})
    out = analyze(
        ["A", "B"], task="t", final_answer="", trace=[], state={},
        _client_factory=factory(raw),
    )
    assert out == {"A": "explanation A"}
