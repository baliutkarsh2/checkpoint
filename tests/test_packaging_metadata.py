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


def test_no_upper_bounds_on_dependencies():
    """A published library must not cap its dependencies.

    An upper bound propagates into every downstream resolver and can make this
    package uninstallable beside a newer release of a shared dependency, so the
    cost of a guess lands on users. Compatibility is proven by testing instead:
    CI resolves the latest of everything across the supported Python versions
    and builds both container images. When a new major genuinely breaks us, the
    fix is to support it (see checkpoint/mcp_compat.py for mcp 1.x/2.x), not to
    pin around it.
    """
    capped = [d for d in PYPROJECT["project"]["dependencies"] if "<" in d]
    assert not capped, (
        f"support the new major instead of capping it: {capped}"
    )


def test_dependency_floors_are_modern():
    """Stale floors make pip backtrack through hundreds of releases.

    A clean install of this graph with year-old floors failed outright with
    pip's `resolution-too-deep`. pip's own guidance for that error is to add
    lower bounds, so these floors are part of the install actually working.
    """
    floors = {
        d.split(">=")[0].split("[")[0]: d.split(">=")[1]
        for d in PYPROJECT["project"]["dependencies"] if ">=" in d
    }
    minimums = {"openai": (2, 0), "pydantic": (2, 9), "httpx": (0, 28), "fastapi": (0, 115)}
    stale = []
    for pkg, want in minimums.items():
        got = tuple(int(x) for x in floors[pkg].split(".")[:2])
        if got < want:
            stale.append(f"{pkg}>={floors[pkg]} (want >={'.'.join(map(str, want))})")
    assert not stale, f"these floors are stale enough to slow resolution: {stale}"


def test_no_empty_optional_dependency_groups():
    """An extra that installs nothing is still advertised on PyPI."""
    empty = [k for k, v in PYPROJECT["project"].get("optional-dependencies", {}).items() if not v]
    assert not empty, f"empty extras are published as installable but do nothing: {empty}"


def test_changelog_exists_and_is_linked():
    assert (REPO_ROOT / "CHANGELOG.md").is_file()
    assert "Changelog" in PYPROJECT["project"]["urls"]


def test_pytest_plugin_does_not_import_heavy_modules_at_startup():
    """The pytest11 entry point loads in every environment that installs us.

    Nothing here may cost real import time, because the price is paid by every
    pytest run in every project that has checkpoint-agents installed — including
    the ones that never write a Checkpoint test. The fixtures import what they
    need inside their own bodies instead.
    """
    import ast

    src = (REPO_ROOT / "checkpoint" / "pytest_plugin.py").read_text(encoding="utf-8")
    top: list[str] = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `if TYPE_CHECKING:` imports never run, so they are free.
            top.append(node.module or "")
    heavy = ("httpx", "uvicorn", "fastapi", "openai",
             "checkpoint", "checkpoint.engine", "checkpoint.twins", "checkpoint.twins.sessions")
    offenders = [name for name in heavy if name in top]
    assert not offenders, (
        f"{offenders} imported at module scope; that cost lands on every pytest "
        "run in any project that installs checkpoint-agents"
    )


# Packages that must never enter the dependency graph, and the reason each one
# is barred. These are not rivals or alternatives — they are libraries whose
# own pins reach back into our graph and move versions out from under us, which
# is a packaging fact, not an opinion about the library.
BARRED_DEPENDENCIES = {
    # Pins exact h11/h2 versions and caps typing-extensions, so merely
    # co-installing it (even as an extra, even in dev) silently downgraded
    # other packages — mcp was forced back to 1.x, hiding a real TypeError.
    # TLS interception is checkpoint/proxy's job anyway.
    "mitmproxy": "pins h11/h2 exactly and caps typing-extensions",
}


def test_barred_packages_are_not_dependencies_anywhere():
    """A package barred for pinning our graph must not return through any group."""
    groups = {"dependencies": PYPROJECT["project"]["dependencies"],
              **PYPROJECT["project"].get("optional-dependencies", {})}
    offenders = [
        f"{package} in [{group}] ({reason})"
        for package, reason in BARRED_DEPENDENCIES.items()
        for group, deps in groups.items()
        if package in " ".join(deps)
    ]
    assert not offenders, "barred dependencies are back: " + "; ".join(offenders)


def test_intercept_proxy_dependencies_are_declared():
    """checkpoint/proxy imports these directly, so they must not rely on being transitive."""
    core = {d.split(">=")[0] for d in PYPROJECT["project"]["dependencies"]}
    assert {"h11", "cryptography", "certifi", "httpx"} <= core
