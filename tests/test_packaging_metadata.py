"""Packaging metadata a published distribution is judged on.

The version in particular: it is declared once (``checkpoint.__version__``) and
derived everywhere else. Before that, it lived in two files with nothing keeping
them in sync and nothing tying either to the pushed git tag — so `git tag v0.2.0`
would have published 0.1.0.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import checkpoint

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_has_a_single_source_of_truth():
    """pyproject must derive the version from the package, not restate it."""
    project = PYPROJECT["project"]
    assert "version" not in project, (
        "pyproject hard-codes a version; it must use dynamic = ['version'] so "
        "checkpoint.__version__ is the only place a version is written"
    )
    assert "version" in project.get("dynamic", [])
    attr = PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "checkpoint.__version__"


def test_version_is_pep440_and_importable():
    v = checkpoint.__version__
    assert v and v[0].isdigit(), f"unexpected version: {v!r}"


def test_py_typed_marker_ships():
    """The annotations are only visible downstream if py.typed is packaged."""
    assert (REPO_ROOT / "checkpoint" / "py.typed").is_file()
    pkg_data = PYPROJECT["tool"]["setuptools"]["package-data"]["checkpoint"]
    assert "py.typed" in pkg_data
    assert "Typing :: Typed" in PYPROJECT["project"]["classifiers"]


def test_runtime_dependencies_are_bounded():
    """An unbounded dep lets a breaking major install cleanly and break users."""
    unbounded = [
        d for d in PYPROJECT["project"]["dependencies"]
        if ">=" in d and "<" not in d and d.split(">=")[0].strip('"') in {
            "openai", "pydantic", "mitmproxy", "docker", "mcp",
        }
    ]
    assert not unbounded, f"volatile dependencies need an upper bound: {unbounded}"


def test_no_empty_optional_dependency_groups():
    """An extra that installs nothing is still advertised on PyPI."""
    empty = [k for k, v in PYPROJECT["project"].get("optional-dependencies", {}).items() if not v]
    assert not empty, f"empty extras are published as installable but do nothing: {empty}"


def test_changelog_exists_and_is_linked():
    assert (REPO_ROOT / "CHANGELOG.md").is_file()
    assert "Changelog" in PYPROJECT["project"]["urls"]


def test_pytest_plugin_does_not_import_heavy_modules_at_startup():
    """The pytest11 entry point loads in every environment that installs us."""
    import ast

    src = (REPO_ROOT / "checkpoint" / "pytest_plugin.py").read_text(encoding="utf-8")
    top: list[str] = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(node.module or "")
    for heavy in ("httpx", "checkpoint.clone_manager"):
        assert heavy not in top, (
            f"{heavy} is imported at module scope; it would be imported on every "
            "pytest run in any project that installs checkpoint-agents"
        )
