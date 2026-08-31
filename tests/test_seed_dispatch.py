"""Phase 4 plan 02 — named seeds + seed-file dispatch.

These tests cover the scenario-level surface for SCN-06 (`seed:`) and
SCN-07 (`seed-file:`). They use HTTP directly against running twins so we
don't pay for an LLM judge.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from checkpoint.runner import _parse_seed_spec, run_once
from checkpoint.scenario import Scenario, parse

NOOP_HARNESS = textwrap.dedent(
    """
    import json, sys
    sys.stdout.write(json.dumps({"text": "noop"}))
    """
).strip()


@pytest.fixture
def noop_harness(tmp_path: Path) -> Path:
    p = tmp_path / "noop.py"
    p.write_text(NOOP_HARNESS)
    return p


def test_scenario_parses_seed_file_key():
    scn = parse("# x\n## Config\nseed-file: ./gh.json\n")
    assert scn.config.get("seed-file") == "./gh.json"


def test_scenario_parses_per_twin_seed_map():
    scn = parse(
        "# x\n## Config\nclones: github,slack\n"
        "seed: github=small-project, slack=engineering-team\n"
    )
    assert _parse_seed_spec(scn.config.get("seed"), scn.clones) == {
        "github": "small-project",
        "slack": "engineering-team",
    }


def test_named_seed_loads_into_twin(noop_harness):
    """SCN-06: `seed: small-project` populates the twin before harness start."""
    s = Scenario(prompt="ok", config={"clones": "github", "seed": "small-project", "timeout": "30"})
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    # small-project has at least one issue and at least one repo.
    assert (r.state.get("repos") or {}), f"got state keys: {list(r.state.keys())}"


def test_unknown_named_seed_errors(noop_harness):
    s = Scenario(prompt="ok", config={"clones": "github", "seed": "does-not-exist", "timeout": "30"})
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert not r.complete
    assert "does-not-exist" in (r.error or "")


def test_seed_file_replaces_state(tmp_path, noop_harness):
    """SCN-07: `seed-file: ./gh.json` replaces twin state with the file's content."""
    seed_path = tmp_path / "custom_gh.json"
    seed_path.write_text(json.dumps({
        "state": {
            "issues": {
                "9": {"number": 9, "title": "From seed-file", "state": "open", "labels": []},
            }
        }
    }))
    s = Scenario(
        prompt="ok",
        config={"clones": "github", "seed-file": str(seed_path), "timeout": "30"},
    )
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    issues = r.state.get("issues") or {}
    assert any(i.get("title") == "From seed-file" for i in issues.values()), \
        f"got issues={issues}"


def test_seed_file_raw_state(tmp_path, noop_harness):
    """seed-file without a top-level `state` key is treated as raw state."""
    seed_path = tmp_path / "raw.json"
    seed_path.write_text(json.dumps({
        "issues": {
            "7": {"number": 7, "title": "Raw style", "state": "open", "labels": []},
        }
    }))
    s = Scenario(
        prompt="ok",
        config={"clones": "github", "seed-file": str(seed_path), "timeout": "30"},
    )
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    issues = r.state.get("issues") or {}
    assert any(i.get("title") == "Raw style" for i in issues.values()), \
        f"got issues={issues}"


def test_seed_file_per_twin_map(tmp_path, noop_harness):
    """`seed-file: github=./gh.json, slack=./sl.json` dispatches per-twin."""
    gh = tmp_path / "gh.json"
    gh.write_text(json.dumps({
        "state": {"issues": {"1": {"number": 1, "title": "GH-from-file", "state": "open", "labels": []}}}
    }))
    sl = tmp_path / "sl.json"
    sl.write_text(json.dumps({
        "state": {"channels": {"C1": {"id": "C1", "name": "from-file", "is_member": True, "topic": {"value": ""}, "purpose": {"value": ""}}}}
    }))
    s = Scenario(
        prompt="ok",
        config={
            "clones": "github,slack",
            "seed-file": f"github={gh}, slack={sl}",
            "timeout": "30",
        },
    )
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    gh_issues = (r.state["github"].get("issues") or {})
    assert any(i.get("title") == "GH-from-file" for i in gh_issues.values())
    sl_channels = (r.state["slack"].get("channels") or {})
    assert any(c.get("name") == "from-file" for c in sl_channels.values())


def test_seed_file_relative_to_scenario(tmp_path, noop_harness):
    """A relative seed-file path resolves against the scenario's directory."""
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    seed = scn_dir / "seed.json"
    seed.write_text(json.dumps({
        "state": {"issues": {"3": {"number": 3, "title": "rel-resolved", "state": "open", "labels": []}}}
    }))
    scn_path = scn_dir / "x.md"
    scn_path.write_text(
        "# rel test\n## Prompt\nok\n## Config\nclones: github\nseed-file: ./seed.json\ntimeout: 30\n"
    )
    from checkpoint.scenario import parse_file
    s = parse_file(scn_path)
    r = run_once(s, [sys.executable, str(noop_harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    issues = r.state.get("issues") or {}
    assert any(i.get("title") == "rel-resolved" for i in issues.values())
