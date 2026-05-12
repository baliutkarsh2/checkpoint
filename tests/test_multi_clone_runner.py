"""Phase 4 plan 01: multi-clone runner.

These tests exercise `run_once` with a scenario that uses `clones: github,slack,stripe`.
They use a tiny harness that just reads CHECKPOINT_<CLONE>_URL env vars and
echoes the count, so we don't need an LLM judge.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from checkpoint.runner import run_once, _parse_seed_spec
from checkpoint.scenario import Scenario, parse


HARNESS_ECHO = textwrap.dedent(
    """
    import json, os, sys
    out = {
        "base": os.environ.get("CHECKPOINT_BASE_URL"),
        "github": os.environ.get("CHECKPOINT_GITHUB_URL"),
        "slack": os.environ.get("CHECKPOINT_SLACK_URL"),
        "stripe": os.environ.get("CHECKPOINT_STRIPE_URL"),
    }
    # `_extract_final_answer` would strip a top-level `text` field, so we
    # wrap the payload as a JSON-string under `text` to survive the extractor.
    sys.stdout.write(json.dumps({"text": json.dumps(out)}))
    """
).strip()


@pytest.fixture
def echo_harness(tmp_path: Path) -> Path:
    p = tmp_path / "echo.py"
    p.write_text(HARNESS_ECHO)
    return p


def test_parse_seed_spec_single_value():
    assert _parse_seed_spec("small-project", ["github"]) == {"github": "small-project"}
    # Single value applies to first clone only.
    assert _parse_seed_spec("small-project", ["github", "slack"]) == {"github": "small-project"}


def test_parse_seed_spec_per_clone_map():
    out = _parse_seed_spec("github=small-project, slack=engineering-team", ["github", "slack"])
    assert out == {"github": "small-project", "slack": "engineering-team"}


def test_parse_seed_spec_empty():
    assert _parse_seed_spec(None, ["github"]) == {}
    assert _parse_seed_spec("", ["github"]) == {}


def test_parse_seed_spec_unknown_clone_kept():
    # Unknown clones in the map are kept; the runner just ignores them.
    out = _parse_seed_spec("foo=bar", ["github"])
    assert out == {"foo": "bar"}


def test_single_clone_back_compat(echo_harness):
    s = Scenario(prompt="hello", config={"clones": "github", "timeout": "30"})
    r = run_once(s, [sys.executable, str(echo_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    payload = json.loads(r.final_answer)
    assert payload["github"], "CHECKPOINT_GITHUB_URL not set"
    assert payload["base"] == payload["github"], "BASE should match first clone"
    assert payload["slack"] is None
    # Single-clone state stays flat: top-level should have github twin keys.
    assert "repos" in r.state or "issues" in r.state


def test_multi_clone_three_twins(echo_harness):
    s = Scenario(prompt="hello", config={"clones": "github,slack,stripe", "timeout": "30"})
    r = run_once(s, [sys.executable, str(echo_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    payload = json.loads(r.final_answer)
    assert payload["github"], "CHECKPOINT_GITHUB_URL missing"
    assert payload["slack"], "CHECKPOINT_SLACK_URL missing"
    assert payload["stripe"], "CHECKPOINT_STRIPE_URL missing"
    # Three different ports.
    urls = {payload["github"], payload["slack"], payload["stripe"]}
    assert len(urls) == 3, f"expected 3 distinct twin URLs, got {urls}"
    # BASE_URL == first clone.
    assert payload["base"] == payload["github"]
    # Multi-clone state shape is nested {clone: state}.
    assert set(r.state.keys()) >= {"github", "slack", "stripe"}


def test_multi_clone_with_per_twin_seeds(echo_harness):
    s = Scenario(
        prompt="hello",
        config={
            "clones": "github,slack,stripe",
            "seed": "github=small-project, slack=engineering-team, stripe=small-business",
            "timeout": "30",
        },
    )
    r = run_once(s, [sys.executable, str(echo_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    # Confirm seeds actually loaded — small-project should have ≥1 repo.
    gh_state = r.state["github"]
    assert gh_state.get("repos") or gh_state.get("issues"), "github seed did not populate"
    # engineering-team has channels.
    sl_state = r.state["slack"]
    assert sl_state.get("channels"), "slack seed did not populate channels"
    # small-business has customers.
    st_state = r.state["stripe"]
    assert st_state.get("customers"), "stripe seed did not populate customers"


def test_seed_file_inline_state(echo_harness, tmp_path):
    """A seed-file pointing to a JSON file replaces the twin's state."""
    seed_file = tmp_path / "gh_seed.json"
    seed_file.write_text(json.dumps({
        "state": {
            "issues": {
                "1": {"number": 1, "title": "Custom seeded", "state": "open", "labels": []},
            }
        }
    }))
    s = Scenario(
        prompt="hello",
        config={
            "clones": "github",
            "seed-file": str(seed_file),
            "timeout": "30",
        },
    )
    r = run_once(s, [sys.executable, str(echo_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    issues = r.state.get("issues") or {}
    # Single-clone flat state.
    assert any(i.get("title") == "Custom seeded" for i in issues.values()), f"got issues={issues}"


def test_unknown_clone_errors():
    s = Scenario(prompt="hi", config={"clones": "github,fakebook"})
    r = run_once(s, [sys.executable, "-c", "print('{}')"])
    assert not r.complete
    assert "fakebook" in (r.error or "")
