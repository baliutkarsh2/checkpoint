"""Scenario authoring: `checkpoint scenario generate`."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from checkpoint.scenario_gen import generate

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
