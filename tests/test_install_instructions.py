"""Every install instruction must name the real distribution.

`checkpoint` and `checkpoint-eval` are NOT this project on PyPI — `checkpoint`
is an unrelated existing package, so a doc that says `pip install checkpoint`
sends users to install the wrong software and they never get the CLI. This
tripwire keeps the three names from drifting apart again.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# "pip install checkpoint" NOT followed by "-agents".
BAD_INSTALL = re.compile(r"pip install\s+(?:-U\s+|--upgrade\s+)?checkpoint(?!-agents)(?![\w-])")

SCANNED_SUFFIXES = {".md", ".yml", ".yaml", ".py", ".toml", ".txt", ".json"}


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - not a git checkout
        # An sdist, a tarball, or a tree exported for a container has no index
        # to ask. That is not a failing repository, and reporting it as one
        # sends the reader hunting for an install line that does not exist.
        pytest.skip("not a git checkout; install-instruction sweep skipped")
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_distribution_name_is_checkpoint_agents():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "checkpoint-agents"


def test_no_tracked_file_recommends_the_wrong_distribution():
    offenders: list[str] = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        if rel == f"tests/{Path(__file__).name}":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if BAD_INSTALL.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")

    assert not offenders, (
        "These files install the wrong PyPI distribution (use `checkpoint-agents`):\n"
        + "\n".join(offenders)
    )
