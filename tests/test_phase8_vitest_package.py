"""Phase 8 / Plan 02: `@checkpoint/vitest` JS package smoke tests.

We can't run Vitest from Python, but we can prove:
  - `package.json` parses + has the expected fields,
  - `index.js` loads under `node -e` and exports the documented surface,
  - the TS .d.ts file exists and mentions both exported functions.

Skips cleanly if `node` isn't on PATH (CI without node, etc.).

Covers DIST-02.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PKG_DIR = Path(__file__).resolve().parent.parent / "checkpoint-vitest"


def test_package_dir_exists() -> None:
    assert PKG_DIR.is_dir(), f"missing: {PKG_DIR}"


def test_package_json_valid() -> None:
    pkg = json.loads((PKG_DIR / "package.json").read_text())
    assert pkg["name"] == "@checkpoint/vitest"
    assert pkg["main"] == "index.js"
    assert pkg["types"] == "index.d.ts"
    assert "index.js" in pkg["files"]
    assert "index.d.ts" in pkg["files"]
    assert "README.md" in pkg["files"]


def test_index_js_present_and_non_trivial() -> None:
    src = (PKG_DIR / "index.js").read_text()
    assert "withCheckpoint" in src
    assert "resetCheckpointTwins" in src
    assert "module.exports" in src


def test_dts_present() -> None:
    dts = (PKG_DIR / "index.d.ts").read_text()
    assert "export function withCheckpoint" in dts
    assert "export function resetCheckpointTwins" in dts


def test_readme_present() -> None:
    readme = (PKG_DIR / "README.md").read_text()
    assert "@checkpoint/vitest" in readme
    assert "withCheckpoint" in readme


def test_node_loads_module_and_lists_exports() -> None:
    """The acceptance check from the plan: node -e must print both exports."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [
            node,
            "-e",
            "console.log(Object.keys(require('./index.js')).sort().join(','))",
        ],
        cwd=PKG_DIR,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert proc.stdout.strip() == "resetCheckpointTwins,withCheckpoint", proc.stdout


def test_node_exports_are_callable_functions() -> None:
    """`typeof` both exports under node — guards against accidental rename."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [
            node,
            "-e",
            (
                "const m = require('./index.js');"
                "console.log(typeof m.withCheckpoint, typeof m.resetCheckpointTwins);"
            ),
        ],
        cwd=PKG_DIR,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert proc.stdout.strip() == "function function", proc.stdout
