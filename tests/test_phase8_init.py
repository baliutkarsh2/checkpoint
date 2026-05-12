"""Phase 8 / Plan 01: `checkpoint init` scaffolding.

Covers CLI-04 + DIST-01.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint import init as _init


EXPECTED_FILES = [
    ".claude/skills/checkpoint/SKILL.md",
    ".claude/commands/checkpoint-test.md",
    ".checkpoint.json",
    "harness.py",
    "harness.json",
    "scenario.md",
]


def test_scaffold_creates_all_files(tmp_path: Path) -> None:
    result = _init.scaffold(tmp_path)
    assert sorted(result.created) == sorted(EXPECTED_FILES)
    assert result.skipped == []
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).is_file(), f"missing: {rel}"


def test_scaffold_is_idempotent(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    second = _init.scaffold(tmp_path)
    assert second.created == []
    assert sorted(second.skipped) == sorted(EXPECTED_FILES)


def test_scaffold_preserves_user_edits(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    marker = "# user edited this on 2026-05-12\n"
    user_path = tmp_path / "harness.py"
    user_path.write_text(marker + user_path.read_text())
    _init.scaffold(tmp_path)
    assert user_path.read_text().startswith(marker), (
        "second init must NOT overwrite an existing user-edited file"
    )


def test_skill_has_valid_frontmatter(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    skill = (tmp_path / ".claude/skills/checkpoint/SKILL.md").read_text()
    # YAML-ish frontmatter: must start with `---`, end with `---`, contain name + description.
    m = re.match(r"^---\n(.*?)\n---\n", skill, flags=re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    fm = m.group(1)
    assert re.search(r"^name:\s*checkpoint\s*$", fm, flags=re.MULTILINE), fm
    assert re.search(r"^description:\s+", fm, flags=re.MULTILINE), fm


def test_slash_command_has_frontmatter(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    cmd = (tmp_path / ".claude/commands/checkpoint-test.md").read_text()
    assert cmd.startswith("---\n"), "slash command missing frontmatter"
    assert "name: checkpoint-test" in cmd
    assert "description:" in cmd


def test_checkpoint_json_is_valid(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    cfg = json.loads((tmp_path / ".checkpoint.json").read_text())
    assert "clones" in cfg
    assert "harness" in cfg
    assert cfg["harness"]["path"].endswith("harness.py")


def test_harness_json_points_at_harness_py(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    manifest = json.loads((tmp_path / "harness.json").read_text())
    assert manifest["path"] == "harness.py"


def test_harness_template_parses_as_python(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    src = (tmp_path / "harness.py").read_text()
    compile(src, "harness.py", "exec")  # raises SyntaxError if invalid


def test_starter_scenario_has_required_sections(tmp_path: Path) -> None:
    _init.scaffold(tmp_path)
    scn = (tmp_path / "scenario.md").read_text()
    for header in ("## Setup", "## Prompt", "## Success Criteria", "## Config"):
        assert header in scn, f"scenario.md missing {header}"


def test_init_cli_command(tmp_path: Path) -> None:
    """End-to-end: invoke `python -m checkpoint.cli init <dir>` and inspect output."""
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.cli", "init", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).is_file(), f"missing after CLI run: {rel}"
    assert "Initialized Checkpoint" in proc.stdout
