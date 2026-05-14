"""Tests for checkpoint.failure_analysis — post-run LLM failure explainer."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from checkpoint.failure_analysis import analyze_failures


# ---------------------------------------------------------------------------
# Fake OpenAI client
# ---------------------------------------------------------------------------

def _fake_client(analyses: list[dict]) -> MagicMock:
    payload = json.dumps({"analyses": analyses})
    choice = SimpleNamespace(message=SimpleNamespace(content=payload))
    resp = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_failed_criteria_returns_empty():
    result = analyze_failures("task", [], [], {})
    assert result == {}


def test_analyze_failures_returns_dict():
    client = _fake_client([
        {
            "criterion": "At least 1 issue exists",
            "why_failed": "Agent made no POST calls.",
            "suggestion": "Call POST /repos/owner/repo/issues.",
        }
    ])
    result = analyze_failures(
        task="Create an issue",
        failed_criteria=["At least 1 issue exists"],
        trace=[],
        state={},
        _client_factory=lambda: client,
    )
    assert isinstance(result, dict)
    assert "At least 1 issue exists" in result
    assert "Agent made no POST calls" in result["At least 1 issue exists"]


def test_analyze_failures_includes_suggestion():
    client = _fake_client([
        {
            "criterion": "PR is merged",
            "why_failed": "No PATCH calls found.",
            "suggestion": "Call PATCH /pulls/N/merge.",
        }
    ])
    result = analyze_failures(
        task="Merge a PR",
        failed_criteria=["PR is merged"],
        trace=[{"method": "GET", "path": "/pulls"}],
        state={},
        _client_factory=lambda: client,
    )
    assert "PR is merged" in result
    text = result["PR is merged"]
    assert "Suggestion" in text or "PATCH" in text


def test_analyze_failures_multiple_criteria():
    client = _fake_client([
        {"criterion": "Issue A exists", "why_failed": "A missing.", "suggestion": "Create A."},
        {"criterion": "Issue B exists", "why_failed": "B missing.", "suggestion": "Create B."},
    ])
    result = analyze_failures(
        task="Create two issues",
        failed_criteria=["Issue A exists", "Issue B exists"],
        trace=[],
        state={},
        _client_factory=lambda: client,
    )
    assert len(result) == 2
    assert "Issue A exists" in result
    assert "Issue B exists" in result


def test_analyze_failures_bad_json_returns_empty():
    """If LLM returns garbage, analyze_failures returns {} gracefully."""
    bad_choice = SimpleNamespace(message=SimpleNamespace(content="not json at all"))
    bad_resp = SimpleNamespace(choices=[bad_choice])
    client = MagicMock()
    client.chat.completions.create.return_value = bad_resp

    result = analyze_failures(
        task="t",
        failed_criteria=["crit"],
        trace=[],
        state={},
        _client_factory=lambda: client,
    )
    assert result == {}


def test_analyze_failures_empty_analyses_key():
    """LLM returns valid JSON but with empty analyses array."""
    client = _fake_client([])
    result = analyze_failures(
        task="t",
        failed_criteria=["crit"],
        trace=[],
        state={},
        _client_factory=lambda: client,
    )
    assert result == {}


def test_analyze_failures_skips_items_without_criterion():
    """Items with empty criterion key are skipped."""
    client = _fake_client([
        {"criterion": "", "why_failed": "unknown", "suggestion": ""},
        {"criterion": "Real criterion", "why_failed": "Missing.", "suggestion": "Fix it."},
    ])
    result = analyze_failures(
        task="t",
        failed_criteria=["Real criterion"],
        trace=[],
        state={},
        _client_factory=lambda: client,
    )
    assert list(result.keys()) == ["Real criterion"]
