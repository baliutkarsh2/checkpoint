"""Auto-discover harness directories the dashboard's RunLauncher can use.

A "harness directory" is anything that contains both a Dockerfile (so docker
mode can build it) and a harness.py (so subprocess mode can run it as a
fallback). We scan in this order, deduping by absolute path:

  1. ``<project_dir>/examples/agents/*``   (the bundled reference agents)
  2. ``<project_dir>/harness/``            (the convention from `checkpoint init`)
  3. ``<project_dir>/agents/*``            (a customer convention we'll honor)

The discovered list is cached for 5 seconds so the dashboard doesn't stat
the filesystem on every keystroke in the launcher dropdown.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable


CACHE_TTL_S = 5.0
_cache: tuple[float, list[dict]] | None = None


def discover(project_dir: Path) -> list[dict]:
    """Return a sorted list of {id, name, path, description, source} dicts.

    `path` is always absolute. `id` is a stable kebab-case slug derived from
    `path` so the SPA can use it in URLs / form values without escaping.
    """
    global _cache
    now = time.monotonic()
    if _cache and (now - _cache[0]) < CACHE_TTL_S:
        return _cache[1]
    out: dict[str, dict] = {}  # keyed by absolute path so we dedupe
    for d in _candidate_dirs(project_dir):
        if not _is_harness_dir(d):
            continue
        abs_path = str(d.resolve())
        if abs_path in out:
            continue
        out[abs_path] = {
            "id": _slug(d, project_dir),
            "name": _friendly_name(d.name),
            "path": _relative_to(d, project_dir),
            "abs_path": abs_path,
            "description": _read_description(d),
            "source": _source(d, project_dir),
        }
    items = sorted(out.values(), key=lambda r: (r["source"] != "bundled", r["id"]))
    _cache = (now, items)
    return items


def invalidate_cache() -> None:
    """Force the next discover() call to re-scan. Useful in tests."""
    global _cache
    _cache = None


# ----------------------------------------------------------------------------

def _candidate_dirs(project_dir: Path) -> Iterable[Path]:
    bundled = project_dir / "examples" / "agents"
    if bundled.is_dir():
        yield from sorted(p for p in bundled.iterdir() if p.is_dir())
    harness = project_dir / "harness"
    if harness.is_dir():
        yield harness
    customer = project_dir / "agents"
    if customer.is_dir():
        yield from sorted(p for p in customer.iterdir() if p.is_dir())


def _is_harness_dir(d: Path) -> bool:
    return (d / "Dockerfile").is_file() and (d / "harness.py").is_file()


def _read_description(d: Path) -> str:
    """First non-blank line of README.md, stripped of markdown noise."""
    readme = d / "README.md"
    if not readme.is_file():
        return ""
    try:
        for raw in readme.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lstrip("#").strip()
            if not line:
                continue
            # Drop bold/italic markers but keep content.
            line = re.sub(r"\*+", "", line)
            return line[:200]
    except OSError:
        pass
    return ""


def _friendly_name(dirname: str) -> str:
    return dirname.replace("-", " ").replace("_", " ").title()


def _slug(d: Path, project_dir: Path) -> str:
    rel = _relative_to(d, project_dir)
    return rel.replace("/", "--").replace("\\", "--")


def _relative_to(d: Path, project_dir: Path) -> str:
    try:
        return d.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(d.resolve())


def _source(d: Path, project_dir: Path) -> str:
    rel = _relative_to(d, project_dir)
    if rel.startswith("examples/agents/"):
        return "bundled"
    if rel == "harness":
        return "init"
    return "local"
