"""CLI-05: checkpoint scenario list."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from checkpoint.cli import main, _enumerate_scenarios


SCN_A = """# Hello scenario

## Prompt
Say hi.

## Success Criteria
- [P] Greeting is friendly.

## Config
clones: github
tags: smoke
"""

SCN_B = """# Multi-clone

## Prompt
do stuff.

## Success Criteria
- [D] exactly 1 issue exists

## Config
clones: github, slack
tags: integration, smoke
"""


def test_enumerate_skips_non_scenarios(tmp_path):
    (tmp_path / "a.md").write_text(SCN_A)
    (tmp_path / "b.md").write_text(SCN_B)
    (tmp_path / "README.md").write_text("# Not a scenario\nJust prose.")
    rows = _enumerate_scenarios(tmp_path)
    paths = {r["title"] for r in rows}
    # README.md has no criteria and no scenario structure — skipped.
    assert "Hello scenario" in paths
    assert "Multi-clone" in paths
    # README's "Not a scenario" title has no criteria so should be skipped.
    assert "Not a scenario" not in paths


def test_enumerate_extracts_clones_and_tags(tmp_path):
    (tmp_path / "b.md").write_text(SCN_B)
    rows = _enumerate_scenarios(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Multi-clone"
    assert "github" in r["clones"] and "slack" in r["clones"]
    assert "smoke" in r["tags"]


def test_scenario_list_cli_table(tmp_path):
    (tmp_path / "a.md").write_text(SCN_A)
    runner = CliRunner()
    result = runner.invoke(main, ["scenario", "list", str(tmp_path)])
    assert result.exit_code == 0
    assert "Hello scenario" in result.output


def test_scenario_list_cli_json(tmp_path):
    (tmp_path / "a.md").write_text(SCN_A)
    (tmp_path / "b.md").write_text(SCN_B)
    runner = CliRunner()
    result = runner.invoke(main, ["scenario", "list", str(tmp_path), "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    titles = {r["title"] for r in rows}
    assert titles == {"Hello scenario", "Multi-clone"}


def test_scenario_list_bundled_examples():
    """The 5 bundled example scenarios should all show up."""
    examples_dir = Path(__file__).parent.parent / "example" / "scenarios"
    if not examples_dir.exists():
        return  # skip if examples not shipped
    rows = _enumerate_scenarios(examples_dir)
    assert len(rows) >= 5
