"""Tests for scenario authoring tooling:
  - checkpoint/scenario_gen.py :: generate()
  - checkpoint/cli.py :: scenario coverage, _suggest_reword
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from checkpoint.scenario_gen import generate
from checkpoint.cli import _suggest_reword


# ---------------------------------------------------------------------------
# Fake OpenAI client
# ---------------------------------------------------------------------------

def _fake_client(content: str) -> MagicMock:
    choice = SimpleNamespace(message=SimpleNamespace(content=content))
    resp = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


_MINIMAL_SCENARIO = """\
# Test Scenario

## Setup

A GitHub repo with no existing issues.

## Prompt

Create an issue titled "Test issue" on acme/webapp.

## Success Criteria

- [D] An issue titled "Test issue" exists
- [P] The agent's final answer mentions the issue number.

## Config

clones: github
seed: small-project
runs: 1
timeout: 60
"""


# ---------------------------------------------------------------------------
# generate() — mocked LLM
# ---------------------------------------------------------------------------

def test_generate_returns_string():
    client = _fake_client(_MINIMAL_SCENARIO)
    result = generate("Create an issue", _client_factory=lambda: client)
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_content_from_llm():
    client = _fake_client(_MINIMAL_SCENARIO)
    result = generate("Create an issue", _client_factory=lambda: client)
    assert "Test Scenario" in result
    assert "## Prompt" in result


def test_generate_with_clone_passes_to_user_message():
    client = _fake_client(_MINIMAL_SCENARIO)
    generate("Create a refund", clone="stripe", _client_factory=lambda: client)
    call_kwargs = client.chat.completions.create.call_args[1]
    user_msg = next(m["content"] for m in call_kwargs["messages"] if m["role"] == "user")
    assert "stripe" in user_msg


def test_generate_without_clone_no_extra_text():
    client = _fake_client(_MINIMAL_SCENARIO)
    generate("Create an issue", _client_factory=lambda: client)
    call_kwargs = client.chat.completions.create.call_args[1]
    user_msg = next(m["content"] for m in call_kwargs["messages"] if m["role"] == "user")
    assert "Use clone" not in user_msg


def test_generate_strips_whitespace():
    client = _fake_client("  \n" + _MINIMAL_SCENARIO + "\n  ")
    result = generate("x", _client_factory=lambda: client)
    assert not result.startswith(" ")
    assert not result.endswith(" ")


# ---------------------------------------------------------------------------
# scenario coverage — uses real PATTERNS + tmp_path
# ---------------------------------------------------------------------------

_STAGE1_SCENARIO = """\
# Stage 1 Scenario

## Prompt

Close two issues.

## Success Criteria

- [D] Exactly 2 issues are closed
- [D] An issue titled "Fix me" exists

## Config

clones: github
runs: 1
"""

_FALLTHROUGH_SCENARIO = """\
# Fallthrough Scenario

## Prompt

Write a clear PR.

## Success Criteria

- [D] The PR description is clear and concise

## Config

clones: github
runs: 1
"""


def test_coverage_stage1_hit(tmp_path):
    (tmp_path / "stage1.md").write_text(_STAGE1_SCENARIO, encoding="utf-8")
    from checkpoint.checker import PATTERNS
    from checkpoint.scenario import parse_file

    scn = parse_file(tmp_path / "stage1.md")
    d_criteria = [c for c in scn.criteria if c.kind == "D"]
    assert d_criteria, "Expected [D] criteria in stage1 scenario"
    for crit in d_criteria:
        hit = any(pat.search(crit.text) for pat, _ in PATTERNS)
        assert hit, f"Expected Stage 1 hit for: {crit.text!r}"


def test_coverage_fallthrough(tmp_path):
    (tmp_path / "fallthrough.md").write_text(_FALLTHROUGH_SCENARIO, encoding="utf-8")
    from checkpoint.checker import PATTERNS
    from checkpoint.scenario import parse_file

    scn = parse_file(tmp_path / "fallthrough.md")
    d_criteria = [c for c in scn.criteria if c.kind == "D"]
    assert d_criteria
    for crit in d_criteria:
        hit = any(pat.search(crit.text) for pat, _ in PATTERNS)
        assert not hit, f"Expected Stage 1 miss for: {crit.text!r}"


def test_coverage_json_structure(tmp_path):
    (tmp_path / "s1.md").write_text(_STAGE1_SCENARIO, encoding="utf-8")
    (tmp_path / "ft.md").write_text(_FALLTHROUGH_SCENARIO, encoding="utf-8")

    from click.testing import CliRunner
    from checkpoint.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["scenario", "coverage", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "total_d" in data
    assert "stage1_hits" in data
    assert "stage1_pct" in data
    assert "rows" in data
    assert data["total_d"] >= 3  # 2 from stage1 + 1 from fallthrough


# ---------------------------------------------------------------------------
# _suggest_reword
# ---------------------------------------------------------------------------

def test_suggest_reword_known_noun_issue():
    hint = _suggest_reword("An open issue should be visible")
    assert hint is not None
    assert "issue" in hint


def test_suggest_reword_known_noun_pr():
    hint = _suggest_reword("The pull request was merged successfully")
    assert hint is not None


def test_suggest_reword_unknown_noun():
    hint = _suggest_reword("The blorb is totally active")
    assert hint is None


def test_suggest_reword_case_insensitive():
    hint = _suggest_reword("An ISSUE titled X exists but is unmatched")
    assert hint is not None
