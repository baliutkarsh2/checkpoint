"""Every bundled scenario's named seed must resolve to a shipped seed file.

This guards the exact regression that shipped once already: the seed fixtures
were missing from the wheel, so `seed: small-project` 404'd and every seeded
scenario failed for anyone who installed the package rather than cloning it.
A missing or renamed seed is caught here instead of at a user's first run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from checkpoint.runner import _parse_seed_spec
from checkpoint.scenario import parse_file

REPO_ROOT = Path(__file__).resolve().parent.parent
TWINS_DIR = REPO_ROOT / "checkpoint" / "twins"
SCENARIOS = sorted((REPO_ROOT / "scenarios").rglob("*.md"))


def _seed_dir(clone: str) -> Path:
    # `google-workspace` -> google_workspace_seeds
    return TWINS_DIR / f"{clone.replace('-', '_')}_seeds"


@pytest.mark.skipif(not SCENARIOS, reason="no bundled scenarios")
@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_named_seed_resolves_to_a_shipped_file(path: Path):
    scenario = parse_file(path)
    raw = scenario.config.get("seed") or scenario.config.get("seed_name")
    if not raw:
        return  # scenario doesn't use a named seed

    clones = scenario.clones or []
    for clone, name in _parse_seed_spec(raw, clones).items():
        seed_file = _seed_dir(clone) / f"{name}.json"
        assert seed_file.is_file(), (
            f"{path.name} requests seed '{name}' for twin '{clone}', but "
            f"{seed_file.relative_to(REPO_ROOT)} does not exist"
        )


def test_every_seed_dir_has_at_least_one_fixture():
    """A seed directory that lost its fixtures would break silently."""
    populated = {d.name for d in TWINS_DIR.glob("*_seeds") if any(d.glob("*.json"))}
    empty = {d.name for d in TWINS_DIR.glob("*_seeds")} - populated
    assert not empty, f"seed directories with no fixtures: {sorted(empty)}"
