"""Phase 8 / Plan 03: the bundled scenario library is discoverable.

Whether each scenario is *honest* — states what its seed really loads, checks
what the agent changed, cannot be aced by a do-nothing agent — is
`tests/test_bundled_scenarios.py`. This file covers DIST-03 only: every bundled
scenario parses, carries tags the `--tag` filter can read, and is enumerable
through `checkpoint scenario list`.

Tags are asserted as a set the library must *contain*, not as an exact list, so
adding a scenario or a tag does not break the test; renaming a twin or dropping
a scenario does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint.config import matches_tag
from checkpoint.scenario import parse_file

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"

# The scenarios the library promises: one per twin plus the cross-system and
# adversarial packs. Each maps to the twins it must run against.
EXPECTED_TWINS = {
    "archal-verbatim-github.md": {"github"},
    "discord-adversarial.md": {"discord"},
    "discord-incident-response.md": {"discord"},
    "github-adversarial.md": {"github"},
    "github-happy-path.md": {"github"},
    "github-supabase-product-launch.md": {"github", "supabase"},
    "google-workspace-adversarial.md": {"google-workspace"},
    "google-workspace-email-ops.md": {"google-workspace"},
    "linear-adversarial.md": {"linear"},
    "linear-github-cross-system.md": {"linear", "github"},
    "linear-issue-triage.md": {"linear"},
    "multi-clone-cross-system.md": {"slack", "stripe"},
    "slack-incident-response.md": {"slack"},
    "stripe-refund-controls.md": {"stripe"},
    "supabase-adversarial.md": {"supabase"},
    "supabase-data-ops.md": {"supabase"},
}

ADVERSARIAL = {
    "discord-adversarial.md", "github-adversarial.md", "google-workspace-adversarial.md",
    "linear-adversarial.md", "supabase-adversarial.md",
}


def test_scenarios_dir_exists() -> None:
    assert SCENARIOS_DIR.is_dir(), f"missing: {SCENARIOS_DIR}"


def test_every_promised_scenario_is_present() -> None:
    files = {p.name for p in SCENARIOS_DIR.glob("*.md")}
    missing = set(EXPECTED_TWINS) - files
    assert not missing, f"missing scenarios: {sorted(missing)}"


@pytest.mark.parametrize("fname", sorted(EXPECTED_TWINS))
def test_scenario_parses_with_expected_metadata(fname: str) -> None:
    scn = parse_file(SCENARIOS_DIR / fname)
    assert set(scn.twins) == EXPECTED_TWINS[fname], (
        f"{fname} runs against {scn.twins}, expected {sorted(EXPECTED_TWINS[fname])}"
    )
    assert scn.tags, f"{fname} has no tags:"
    assert scn.prompt, f"{fname} has an empty task"
    assert scn.criteria, f"{fname} has no criteria"


@pytest.mark.parametrize("fname", sorted(ADVERSARIAL))
def test_adversarial_scenarios_are_tagged_and_guarded(fname: str) -> None:
    """The `--tag adversarial` pack has to be findable, and each entry has to
    carry at least one must-pass "do no harm" criterion."""
    scn = parse_file(SCENARIOS_DIR / fname)
    assert "adversarial" in scn.tags, f"{fname} is not tagged adversarial"
    guards = [c for c in scn.must_pass if c.kind in ("D", "T")]
    assert guards, f"{fname} has no must-pass [D!]/[T!] criterion guarding the damage"


def test_tag_filter_finds_the_adversarial_pack() -> None:
    matched = {p.name for p in SCENARIOS_DIR.rglob("*.md")
               if matches_tag(parse_file(p).config.get("tags"), "adversarial")}
    assert ADVERSARIAL <= matched, f"--tag adversarial missed {sorted(ADVERSARIAL - matched)}"


def test_scenario_list_json_returns_the_library() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.cli", "scenario", "list", str(SCENARIOS_DIR), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    rows = json.loads(proc.stdout)
    names = {Path(r["path"]).name for r in rows}
    missing = set(EXPECTED_TWINS) - names
    assert not missing, f"scenario list missing: {sorted(missing)}"
    for row in rows:
        assert row["tags"], f"{row['path']} is listed without tags"


def test_multi_clone_scenarios_use_two_twins() -> None:
    for fname in ("multi-clone-cross-system.md", "linear-github-cross-system.md",
                  "github-supabase-product-launch.md"):
        scn = parse_file(SCENARIOS_DIR / fname)
        assert len(scn.twins) == 2, f"{fname} runs {scn.twins}, expected two twins"
