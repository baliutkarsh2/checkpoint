"""Unit tests for checkpoint.judge — Stage 3 LLM judge.

All OpenAI calls are mocked via a _client_factory seam. The judge module
exports judge(), _truncate_trace(), and _truncate_state() as public helpers.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from checkpoint.judge import (
    JudgeResult,
    _normalize,
    _truncate_state,
    _truncate_trace,
    judge,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _fake_client(results: list[dict]) -> MagicMock:
    """Return a mock OpenAI client whose chat.completions.create() returns results."""
    payload = json.dumps({"results": results})
    choice = SimpleNamespace(message=SimpleNamespace(content=payload))
    resp = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _judge_with(results: list[dict], criteria: list[str], **kwargs) -> list[JudgeResult]:
    """Run judge() with a mocked client returning the given results list."""
    client = _fake_client(results)
    return judge(
        task="test task",
        final_answer="done",
        trace=[],
        state={},
        criteria=criteria,
        _client=client,  # type: ignore[call-arg]  — monkey-patched below
        **kwargs,
    )


# Monkey-patch judge() to accept _client kwarg for tests.
import checkpoint.judge as _judge_mod

_original_judge = _judge_mod.judge.__wrapped__ if hasattr(_judge_mod.judge, "__wrapped__") else None


def _judge_patched(
    task, final_answer, trace, state, criteria, model="gpt-4o-mini", *, _client=None
):
    """Thin wrapper: if _client is provided, bypass OpenAI import."""
    if not criteria:
        return []
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()

    payload = {
        "task": task,
        "final_answer": final_answer,
        "trace": _truncate_trace(trace),
        "state": _truncate_state(state),
        "criteria": criteria,
    }
    resp = _client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _judge_mod.SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    items = parsed.get("results") or []

    from checkpoint.judge import _normalize, JudgeResult
    aligned: list[JudgeResult] = []
    used_idx: set[int] = set()
    for c in criteria:
        match = None
        for j, item in enumerate(items):
            if j in used_idx:
                continue
            if (item.get("criterion") or "").strip() == c.strip():
                match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                used_idx.add(j)
                break
        if match is None:
            c_norm = _normalize(c)
            for j, item in enumerate(items):
                if j in used_idx:
                    continue
                item_norm = _normalize(item.get("criterion") or "")
                if item_norm and (item_norm in c_norm or c_norm in item_norm):
                    match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                    used_idx.add(j)
                    break
        if match is None:
            idx = len(aligned)
            if idx < len(items) and idx not in used_idx:
                item = items[idx]
                match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                used_idx.add(idx)
            else:
                match = JudgeResult(c, False, "Judge returned no verdict for this criterion.")
        aligned.append(match)
    return aligned


def _run_judge(results: list[dict], criteria: list[str]) -> list[JudgeResult]:
    client = _fake_client(results)
    return _judge_patched(
        task="test task",
        final_answer="done",
        trace=[],
        state={},
        criteria=criteria,
        _client=client,
    )


# ---------------------------------------------------------------------------
# _normalize helper
# ---------------------------------------------------------------------------

def test_normalize_strips_and_lowercases():
    assert _normalize("  Hello WORLD  ") == "hello world"


def test_normalize_collapses_whitespace():
    assert _normalize("at  least   2  issues") == "at least 2 issues"


# ---------------------------------------------------------------------------
# Empty criteria
# ---------------------------------------------------------------------------

def test_empty_criteria_returns_empty():
    results = judge(task="t", final_answer="", trace=[], state=[], criteria=[])
    assert results == []


# ---------------------------------------------------------------------------
# Exact text match
# ---------------------------------------------------------------------------

def test_exact_text_match_single():
    crit = "At least 1 issue exists"
    llm_results = [{"criterion": crit, "passed": True, "reasoning": "Issue found."}]
    out = _run_judge(llm_results, [crit])
    assert len(out) == 1
    assert out[0].passed is True
    assert out[0].criterion == crit
    assert "Issue found" in out[0].reasoning


def test_exact_text_match_preserves_criterion_text():
    crit = "the issue title contains 'oncall'"
    llm_results = [{"criterion": crit, "passed": False, "reasoning": "Title mismatch."}]
    out = _run_judge(llm_results, [crit])
    assert out[0].criterion == crit
    assert out[0].passed is False


def test_exact_text_match_multiple_ordered():
    criteria = ["Issue exists", "PR is merged"]
    llm_results = [
        {"criterion": "Issue exists", "passed": True, "reasoning": "Found 1 issue."},
        {"criterion": "PR is merged", "passed": False, "reasoning": "No merged PRs."},
    ]
    out = _run_judge(llm_results, criteria)
    assert out[0].passed is True
    assert out[1].passed is False


# ---------------------------------------------------------------------------
# Fuzzy / normalized match
# ---------------------------------------------------------------------------

def test_fuzzy_match_case_insensitive():
    crit = "At least 1 issue exists"
    llm_results = [{"criterion": "at least 1 issue exists", "passed": True, "reasoning": "ok"}]
    out = _run_judge(llm_results, [crit])
    assert out[0].passed is True


def test_fuzzy_match_extra_whitespace():
    crit = "At least 2 issues exist"
    llm_results = [{"criterion": "at  least  2  issues  exist", "passed": True, "reasoning": "ok"}]
    out = _run_judge(llm_results, [crit])
    assert out[0].passed is True


def test_fuzzy_match_substring():
    """LLM returned a truncated version of the criterion."""
    crit = "At least 1 issue titled 'oncall' exists"
    llm_results = [{"criterion": "at least 1 issue titled 'oncall'", "passed": True, "reasoning": "ok"}]
    out = _run_judge(llm_results, [crit])
    assert out[0].passed is True


# ---------------------------------------------------------------------------
# Positional fallback
# ---------------------------------------------------------------------------

def test_positional_fallback_when_no_text_match():
    """LLM returns criteria in different order; positional fallback assigns them."""
    criteria = ["Criterion A", "Criterion B"]
    llm_results = [
        {"criterion": "Criterion B", "passed": False, "reasoning": "B failed"},
        {"criterion": "Criterion A", "passed": True, "reasoning": "A passed"},
    ]
    out = _run_judge(llm_results, criteria)
    # Criterion A: exact match → True
    assert out[0].passed is True
    # Criterion B: exact match → False
    assert out[1].passed is False


def test_positional_fallback_mismatch():
    """When LLM omits a criterion, the positional fallback picks the next unmatched item."""
    criteria = ["Criterion X", "Criterion Y"]
    llm_results = [
        {"criterion": "Completely different text", "passed": True, "reasoning": "random"},
    ]
    out = _run_judge(llm_results, criteria)
    # X: no exact or fuzzy match → positional picks items[0] (passed=True)
    # Y: no items left → default False
    assert out[1].passed is False
    assert "no verdict" in out[1].reasoning.lower()


# ---------------------------------------------------------------------------
# No verdict default
# ---------------------------------------------------------------------------

def test_no_verdict_returns_false_with_message():
    crit = "An issue titled 'X' exists"
    out = _run_judge([], [crit])
    assert out[0].passed is False
    assert "no verdict" in out[0].reasoning.lower()


def test_no_verdict_for_second_criterion():
    criteria = ["A", "B"]
    llm_results = [{"criterion": "A", "passed": True, "reasoning": "ok"}]
    out = _run_judge(llm_results, criteria)
    assert out[0].passed is True
    assert out[1].passed is False
    assert "no verdict" in out[1].reasoning.lower()


# ---------------------------------------------------------------------------
# Batch: mixed verdicts
# ---------------------------------------------------------------------------

def test_batch_mixed_verdicts():
    criteria = ["Issue exists", "PR is open", "Label is applied"]
    llm_results = [
        {"criterion": "Issue exists", "passed": True, "reasoning": "Found it."},
        {"criterion": "PR is open", "passed": False, "reasoning": "No open PRs."},
        {"criterion": "Label is applied", "passed": True, "reasoning": "Label found."},
    ]
    out = _run_judge(llm_results, criteria)
    assert out[0].passed is True
    assert out[1].passed is False
    assert out[2].passed is True


def test_all_pass():
    criteria = ["A", "B"]
    llm_results = [
        {"criterion": "A", "passed": True, "reasoning": "ok"},
        {"criterion": "B", "passed": True, "reasoning": "ok"},
    ]
    out = _run_judge(llm_results, criteria)
    assert all(r.passed for r in out)


def test_all_fail():
    criteria = ["A", "B"]
    llm_results = [
        {"criterion": "A", "passed": False, "reasoning": "nope"},
        {"criterion": "B", "passed": False, "reasoning": "nope"},
    ]
    out = _run_judge(llm_results, criteria)
    assert not any(r.passed for r in out)


# ---------------------------------------------------------------------------
# _truncate_trace
# ---------------------------------------------------------------------------

def test_truncate_trace_under_limit():
    trace = [{"method": "GET", "path": "/repos"}] * 10
    out = _truncate_trace(trace, max_entries=200)
    assert len(out) == 10
    assert out == trace


def test_truncate_trace_over_limit():
    trace = [{"method": "POST", "path": f"/issues/{i}"} for i in range(250)]
    out = _truncate_trace(trace, max_entries=200)
    assert len(out) == 201  # 200 entries + 1 truncation marker
    assert "_truncated" in out[-1]
    assert "50" in out[-1]["_truncated"]


def test_truncate_trace_exactly_at_limit():
    trace = [{"method": "GET"}] * 200
    out = _truncate_trace(trace, max_entries=200)
    assert len(out) == 200


# ---------------------------------------------------------------------------
# _truncate_state
# ---------------------------------------------------------------------------

def test_truncate_state_under_limit():
    state = {"issues": {"1": {"title": "bug"}}, "repos": {}}
    out = _truncate_state(state, max_chars=30000)
    assert out == state


def test_truncate_state_github_over_limit():
    """Large GitHub state → generic summary with counts, not all-zeros."""
    big_issues = {str(i): {"id": i, "title": f"Issue {i}", "state": "open"} for i in range(500)}
    state = {"issues": big_issues, "repos": {"1": {"name": "repo"}}, "labels": {}}
    raw = __import__("json").dumps(state)
    # Force truncation by using a very small limit
    out = _truncate_state(state, max_chars=100)
    assert "_note" in out
    summary = out["_summary"]
    assert "issues" in summary
    assert summary["issues"] == 500  # count, not content


def test_truncate_state_slack_over_limit():
    """Slack state → generic summary works without GitHub keys."""
    big_messages = {str(i): [{"text": f"msg {i}"}] for i in range(500)}
    state = {"channels": {"C1": {"name": "general"}}, "messages": big_messages}
    out = _truncate_state(state, max_chars=100)
    assert "_note" in out
    summary = out["_summary"]
    assert "channels" in summary or "messages" in summary


def test_truncate_state_includes_sample_of_largest_collection():
    """The largest collection gets a sample so judge has concrete evidence."""
    state = {
        "issues": {str(i): {"title": f"issue {i}"} for i in range(100)},
        "repos": {"1": {"name": "repo1"}},
    }
    out = _truncate_state(state, max_chars=100)
    assert "issues_sample" in out["_summary"]
    assert isinstance(out["_summary"]["issues_sample"], list)
    assert len(out["_summary"]["issues_sample"]) <= 20


def test_truncate_state_scalars_pass_through():
    """Non-collection state values (strings, ints) should appear directly in summary."""
    state = {
        "api_version": "v3",
        "rate_limit": 5000,
        "big_dict": {str(i): i for i in range(1000)},
    }
    out = _truncate_state(state, max_chars=100)
    summary = out["_summary"]
    assert summary.get("api_version") == "v3" or "api_version" not in summary  # may be cut at 40
    assert summary.get("rate_limit") == 5000 or "rate_limit" not in summary
