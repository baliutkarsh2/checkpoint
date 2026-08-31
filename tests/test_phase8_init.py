"""`checkpoint init` — declarative (v2 zero-code) AND legacy template paths.

v0.3 changed the default behavior: instead of copying a `harness.py`
template, init writes a declarative `harness.json`. The legacy template
path is still supported via `--template`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint import init as _init

# Zero-code (default) layout: no harness.py, scenario at scenarios/quickstart.md.
EXPECTED_ZERO_CODE = [
    ".claude/skills/checkpoint/SKILL.md",
    ".claude/commands/checkpoint-test.md",
    ".checkpoint.json",
    "scenarios/quickstart.md",
    "harness.json",
    ".gitignore (appended)",
]

EXPECTED_ZERO_CODE_FILES_ON_DISK = [
    ".claude/skills/checkpoint/SKILL.md",
    ".claude/commands/checkpoint-test.md",
    ".checkpoint.json",
    "scenarios/quickstart.md",
    "harness.json",
    ".gitignore",
]

# Legacy template layout (--template raw): writes harness.py at the root.
EXPECTED_LEGACY_TEMPLATE_FILES = [
    ".claude/skills/checkpoint/SKILL.md",
    ".claude/commands/checkpoint-test.md",
    ".checkpoint.json",
    "scenarios/quickstart.md",
    "harness.py",
    ".gitignore",
]


# ---------------------------------------------------------------------------
# Zero-code (default) path
# ---------------------------------------------------------------------------

def test_zero_code_scaffold_writes_harness_json_not_python(tmp_path: Path) -> None:
    result = _init.scaffold(tmp_path, command="python my_agent.py")
    assert result.mode == "zero-code"
    assert sorted(result.created) == sorted(EXPECTED_ZERO_CODE)
    for rel in EXPECTED_ZERO_CODE_FILES_ON_DISK:
        assert (tmp_path / rel).is_file(), f"missing: {rel}"
    # Critically: NO Python file gets written into the user's repo.
    assert not (tmp_path / "harness.py").exists()


def test_harness_json_v2_shape(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python my_agent.py")
    manifest = json.loads((tmp_path / "harness.json").read_text())
    assert manifest["version"] == 2
    assert manifest["command"] == "python my_agent.py"
    # Defaults are omitted for clarity (task_via=env is implicit).
    assert "task_via" not in manifest


def test_harness_json_arg_mode(tmp_path: Path) -> None:
    _init.scaffold(
        tmp_path,
        command="node agent.js",
        task_via="arg",
        task_arg="--prompt",
    )
    manifest = json.loads((tmp_path / "harness.json").read_text())
    assert manifest["task_via"] == "arg"
    assert manifest["task_arg"] == "--prompt"


def test_harness_json_with_dockerfile(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py", dockerfile="./Dockerfile")
    manifest = json.loads((tmp_path / "harness.json").read_text())
    assert manifest["docker"] == {"dockerfile": "./Dockerfile"}


def test_scaffold_is_idempotent(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    second = _init.scaffold(tmp_path, command="python a.py")
    assert second.created == [], (
        f"second init should skip everything, got created={second.created}"
    )


def test_existing_harness_json_preserved(tmp_path: Path) -> None:
    """If the user already has a harness.json, init must NOT overwrite it."""
    _init.scaffold(tmp_path, command="python a.py")
    user_marker = '{"command": "MY-CUSTOM-COMMAND"}\n'
    (tmp_path / "harness.json").write_text(user_marker)
    _init.scaffold(tmp_path, command="python different.py")
    assert (tmp_path / "harness.json").read_text() == user_marker


def test_starter_scenario_has_required_sections(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    scn = (tmp_path / "scenarios/quickstart.md").read_text()
    for header in ("## Prompt", "## Success Criteria", "## Config"):
        assert header in scn, f"scenario missing {header}"


def test_gitignore_entry_added(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    gi = (tmp_path / ".gitignore").read_text()
    assert ".checkpoint/" in gi


def test_gitignore_not_duplicated(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    _init.scaffold(tmp_path, command="python a.py")
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count(".checkpoint/") == 1


def test_banner_says_no_python_written(tmp_path: Path) -> None:
    result = _init.scaffold(tmp_path, command="python a.py")
    assert "No Python file was written" in result.banner
    assert "python a.py" in result.banner


# ---------------------------------------------------------------------------
# Legacy template path
# ---------------------------------------------------------------------------

def test_legacy_template_writes_harness_py(tmp_path: Path) -> None:
    result = _init.scaffold(tmp_path, template="raw")
    assert result.mode == "python-template"
    assert (tmp_path / "harness.py").is_file()
    # Should NOT also write a v2 harness.json.
    assert not (tmp_path / "harness.json").exists()


def test_legacy_template_parses_as_python(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, template="raw")
    src = (tmp_path / "harness.py").read_text()
    compile(src, "harness.py", "exec")


def test_command_and_template_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _init.scaffold(tmp_path, command="python a.py", template="raw")


# ---------------------------------------------------------------------------
# Skill / slash command files
# ---------------------------------------------------------------------------

def test_skill_has_valid_frontmatter(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    skill = (tmp_path / ".claude/skills/checkpoint/SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n", skill, flags=re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    fm = m.group(1)
    assert re.search(r"^name:\s*checkpoint\s*$", fm, flags=re.MULTILINE), fm
    assert re.search(r"^description:\s+", fm, flags=re.MULTILINE), fm


def test_slash_command_has_frontmatter(tmp_path: Path) -> None:
    _init.scaffold(tmp_path, command="python a.py")
    cmd = (tmp_path / ".claude/commands/checkpoint-test.md").read_text()
    assert cmd.startswith("---\n")
    assert "name: checkpoint-test" in cmd
    assert "description:" in cmd


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def test_init_cli_command_zero_code(tmp_path: Path) -> None:
    """Default CLI invocation produces zero-code layout."""
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.cli", "init", str(tmp_path),
         "--command", "python my_agent.py"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (tmp_path / "harness.json").is_file()
    assert not (tmp_path / "harness.py").exists()
    assert "No Python file was written" in proc.stdout


def test_init_cli_with_template(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.cli", "init", str(tmp_path),
         "--template", "raw"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (tmp_path / "harness.py").is_file()
