"""Build (or generate-then-build) a Docker image for the agent harness.

Detection order:
  1. Dockerfile in harness_dir -> build it.
  2. requirements.txt or pyproject.toml -> generate Python Dockerfile.
  3. package.json -> generate Node Dockerfile.
  4. otherwise -> HarnessImageError.

The generated Dockerfile is written to a tmp build context so the user's
repo is never mutated.
"""
from __future__ import annotations

import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import docker
from docker.errors import BuildError, APIError


class HarnessImageError(RuntimeError):
    pass


_PY_DOCKERFILE = """\
FROM python:3.11-slim
WORKDIR /harness
COPY . /harness
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; \\
    elif [ -f pyproject.toml ]; then pip install --no-cache-dir .; fi
CMD ["python", "{entry}"]
"""

_NODE_DOCKERFILE = """\
FROM node:20-slim
WORKDIR /harness
COPY . /harness
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi
CMD ["node", "{entry}"]
"""


def _entry_file(harness_entry: Optional[str], default: str) -> str:
    if not harness_entry:
        return default
    parts = shlex.split(harness_entry)
    # 'python harness.py' -> harness.py; 'node agent.mjs' -> agent.mjs
    if len(parts) >= 2 and parts[0] in ("python", "python3", "node", "node20"):
        return parts[1]
    # Just a path / single-token command: use as-is.
    return parts[-1]


def _detect_runtime(harness_dir: Path) -> str:
    if (harness_dir / "Dockerfile").exists():
        return "dockerfile"
    if (harness_dir / "requirements.txt").exists() or (harness_dir / "pyproject.toml").exists():
        return "python"
    if (harness_dir / "package.json").exists():
        return "node"
    return "unknown"


def build_harness_image(
    harness_dir: Path,
    harness_entry: Optional[str],
    tag: str,
) -> str:
    harness_dir = Path(harness_dir).resolve()
    if not harness_dir.is_dir():
        raise HarnessImageError(f"harness_dir does not exist: {harness_dir}")

    runtime = _detect_runtime(harness_dir)
    if runtime == "unknown":
        raise HarnessImageError(
            f"No Dockerfile, requirements.txt, pyproject.toml, or package.json found in {harness_dir}. "
            "Add one (or write a Dockerfile) so checkpoint can build the harness image."
        )

    client = docker.from_env()

    if runtime == "dockerfile":
        try:
            image, _logs = client.images.build(
                path=str(harness_dir), dockerfile="Dockerfile", tag=tag, rm=True,
            )
        except (BuildError, APIError) as e:
            raise HarnessImageError(f"docker build failed: {e}") from e
        return tag

    # Generate into a tmp context so we don't mutate the user's repo.
    with tempfile.TemporaryDirectory(prefix="checkpoint-harness-") as tmp:
        ctx = Path(tmp)
        # Copy harness_dir into ctx (skip hidden dirs to keep build context small).
        for item in harness_dir.iterdir():
            if item.name.startswith(".") or item.name in {"__pycache__", "node_modules"}:
                continue
            dst = ctx / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)

        if runtime == "python":
            entry = _entry_file(harness_entry, "harness.py")
            (ctx / "Dockerfile").write_text(_PY_DOCKERFILE.format(entry=entry))
        else:  # node
            entry = _entry_file(harness_entry, "index.js")
            (ctx / "Dockerfile").write_text(_NODE_DOCKERFILE.format(entry=entry))

        try:
            image, _logs = client.images.build(
                path=str(ctx), dockerfile="Dockerfile", tag=tag, rm=True,
            )
        except (BuildError, APIError) as e:
            raise HarnessImageError(f"docker build failed: {e}") from e

    return tag
