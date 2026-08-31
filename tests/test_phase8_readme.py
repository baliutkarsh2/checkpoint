"""Phase 8 / Plan 04: README quickstart shape.

Covers DIST-04 — README must be skim-able with the required sections.

The line cap was 120 in Phase 8 when the README only documented the CLI.
With the v0.1.0 dashboard release the README also documents `checkpoint serve`,
the SPA, the JSON API surface, the security model, and the npm dev workflow.
A 200-line cap still keeps it skim-able while leaving room to document a real
product surface.
"""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
README_MAX_LINES = 200


def test_readme_under_max_lines() -> None:
    lines = README.read_text().splitlines()
    assert len(lines) < README_MAX_LINES, (
        f"README is {len(lines)} lines, must be < {README_MAX_LINES}"
    )


def test_readme_has_install_section() -> None:
    text = README.read_text()
    assert "\n## Install\n" in text, "README missing `## Install`"


def test_readme_has_quickstart_section() -> None:
    text = README.read_text()
    # Allow `## Quickstart`, `## Quickstart — your own agent`, etc. — the
    # README has multiple Quickstart variants since v0.2 (one per onboarding path).
    assert "\n## Quickstart" in text, "README missing `## Quickstart` heading"


def test_readme_has_mental_model_section() -> None:
    text = README.read_text()
    assert "\n## Mental model\n" in text, "README missing `## Mental model`"


def test_readme_mentions_checkpoint_init() -> None:
    text = README.read_text()
    assert "checkpoint init" in text


def test_readme_mentions_first_scenario_command() -> None:
    text = README.read_text()
    assert "checkpoint run" in text


def test_readme_one_line_pitch_present() -> None:
    """First non-header non-empty line should be a single-line pitch."""
    body = [
        line for line in README.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # The pitch is the first non-header body line; assert it leads with the
    # core value prop — the release gate for agents (SHIP/BLOCK in CI).
    pitch = body[0]
    lowered = pitch.lower()
    assert "agent" in lowered or "test" in lowered
    assert any(s in lowered for s in ("gate", "ship", "block", "ci"))
