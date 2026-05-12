"""Phase 8 / Plan 03: bundled scenario library.

Verifies the 5 hand-written scenarios under `scenarios/` parse correctly,
have `tags:` set, and are enumerable via `checkpoint scenario list`.

Covers DIST-03.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint.scenario import parse_file


SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"

EXPECTED = {
    "github-happy-path.md": {"tags": {"happy-path", "github"}, "clones": {"github"}},
    "github-adversarial.md": {"tags": {"adversarial", "github"}, "clones": {"github"}},
    "slack-incident-response.md": {"tags": {"slack", "incident"}, "clones": {"slack"}},
    "stripe-refund-controls.md": {
        "tags": {"stripe", "financial-controls"},
        "clones": {"stripe"},
    },
    "multi-clone-cross-system.md": {
        "tags": {"multi-clone", "cross-system"},
        "clones": {"slack", "stripe"},
    },
}


def _split_tags(raw: str) -> set[str]:
    return {t.strip() for t in raw.split(",") if t.strip()}


def test_scenarios_dir_exists() -> None:
    assert SCENARIOS_DIR.is_dir(), f"missing: {SCENARIOS_DIR}"


def test_all_five_scenarios_present() -> None:
    files = {p.name for p in SCENARIOS_DIR.glob("*.md")}
    missing = set(EXPECTED) - files
    assert not missing, f"missing scenarios: {missing}"


@pytest.mark.parametrize("fname", sorted(EXPECTED.keys()))
def test_scenario_parses_with_expected_metadata(fname: str) -> None:
    scn = parse_file(SCENARIOS_DIR / fname)
    expected = EXPECTED[fname]
    assert _split_tags(scn.config.get("tags", "")) == expected["tags"], (
        f"{fname} tags mismatch: got {scn.config.get('tags')!r}"
    )
    assert set(scn.clones) == expected["clones"], (
        f"{fname} clones mismatch: got {scn.clones}"
    )
    # Every scenario must have a prompt + at least one criterion.
    assert scn.prompt, f"{fname} has empty prompt"
    assert scn.criteria, f"{fname} has no success criteria"


def test_scenario_list_json_returns_all_five() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.cli", "scenario", "list", str(SCENARIOS_DIR), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    rows = json.loads(proc.stdout)
    names = {Path(r["path"]).name for r in rows}
    assert names == set(EXPECTED), f"scenario list returned: {names}"
    # Every row should have non-empty tags.
    for r in rows:
        assert r["tags"], f"{r['path']} missing tags in `scenario list` output"


def test_tag_filter_skips_non_matching() -> None:
    """`--tag adversarial` should match exactly one scenario."""
    # Use checkpoint scenario list (which doesn't filter by tag), then check
    # that parse_file picks up tags matching the filter. We're proxying for
    # the CLI tag-filter path that already has its own tests.
    matches = []
    for fname in EXPECTED:
        scn = parse_file(SCENARIOS_DIR / fname)
        if "adversarial" in _split_tags(scn.config.get("tags", "")):
            matches.append(fname)
    assert matches == ["github-adversarial.md"]


def test_multi_clone_scenario_uses_two_clones() -> None:
    scn = parse_file(SCENARIOS_DIR / "multi-clone-cross-system.md")
    assert len(scn.clones) == 2, f"expected 2 clones, got {scn.clones}"
