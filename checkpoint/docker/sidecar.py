"""Build the TLS-sidecar image on first use.

The docker-mode runner needs a `checkpoint-sidecar:latest` image (a container
running Checkpoint's intercept proxy, which mints a CA at startup and routes
intercepted SaaS domains to the local twins). Historically nothing built it, so
the default `checkpoint run` failed on a clean machine with `ImageNotFound`.
`ensure_sidecar_image()` fixes that: it builds the image once, transparently,
the first time it is needed.

The sidecar Dockerfile (`checkpoint/proxy/Dockerfile`) expects a build context
holding `pyproject.toml` + the `checkpoint` package it copies into the image.
We support both ways Checkpoint can be installed:

  * source checkout — pyproject.toml sits at the repo root next to the package;
    build straight from there.
  * installed wheel — pyproject.toml isn't on disk, so we assemble a temporary
    build context from the installed package plus a minimal generated
    pyproject.toml.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import checkpoint

log = logging.getLogger("checkpoint.docker.sidecar")

# Single source of truth for the sidecar tag (docker/runner.py imports this).
SIDECAR_IMAGE = os.environ.get("CHECKPOINT_SIDECAR_IMAGE", "checkpoint-sidecar:latest")

# The runner <-> sidecar contract: how the sidecar is configured
# (CHECKPOINT_ROUTES) and how it signals readiness (a "ready" line). It is
# stamped into the image as a label, and an image built for another contract —
# e.g. a cached mitmproxy-era image, which never prints "ready" — counts as
# absent, so it is rebuilt instead of failing every run. Bump it together with
# the LABEL in checkpoint/proxy/Dockerfile whenever that contract changes.
SIDECAR_CONTRACT = "intercept-proxy-1"
SIDECAR_CONTRACT_LABEL = "dev.checkpoint.sidecar-contract"

_PKG_DIR = Path(checkpoint.__file__).resolve().parent          # .../checkpoint
_PROXY_DOCKERFILE_REL = "checkpoint/proxy/Dockerfile"           # relative to a build context root

# Fallback runtime deps, mirroring pyproject.toml, used only when installed
# metadata can't be read (e.g. an odd editable layout).
_FALLBACK_REQUIREMENTS = [
    "fastapi>=0.115", "uvicorn[standard]>=0.31.1", "httpx>=0.28", "click>=8.1.7",
    "rich>=13.7", "openai>=2.0", "pydantic>=2.9", "python-dotenv>=1.0",
    "docker>=7.1", "mcp>=1.27", "sse-starlette>=2.1",
    "h11>=0.16", "cryptography>=44.0", "certifi>=2025.1.31",
]


def sidecar_image_exists(client, tag: str = SIDECAR_IMAGE) -> bool:
    """True if the image is present locally and speaks the current runner contract."""
    try:
        image = client.images.get(tag)
    except Exception:
        return False
    return (image.labels or {}).get(SIDECAR_CONTRACT_LABEL) == SIDECAR_CONTRACT


def _find_source_root() -> Path | None:
    """Return the repo root if we're running from a source checkout.

    A valid build context has pyproject.toml next to the checkpoint package.
    """
    root = _PKG_DIR.parent
    if (root / "pyproject.toml").exists() and (root / "checkpoint").is_dir():
        return root
    return None


def _runtime_requirements() -> list[str]:
    """Runtime dependencies for the generated pyproject stub.

    A source checkout is the truth when there is one: installed metadata is a
    snapshot from install time, so a dependency added since then would be
    missing from the sidecar image and only fail when the container runs.
    """
    from_source = _requirements_from_source()
    if from_source:
        return from_source
    for dist in ("checkpoint-agents", "checkpoint"):
        try:
            reqs = metadata.requires(dist)
        except metadata.PackageNotFoundError:
            continue
        if not reqs:
            continue
        # Core deps only: they already include everything the sidecar runs
        # (the intercept proxy's h11/cryptography/certifi and the twins'
        # stack); extras such as `dev` carry an `extra ==` marker.
        out = [r.split(";", 1)[0].strip() for r in reqs if "extra ==" not in r]
        if out:
            return out
    return list(_FALLBACK_REQUIREMENTS)


def _requirements_from_source() -> list[str]:
    """Dependencies from the repository's pyproject.toml, when running from one."""
    pyproject = _PKG_DIR.parent / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    deps = data.get("project", {}).get("dependencies")
    return [str(d) for d in deps] if isinstance(deps, list) else []


def _assemble_wheel_context(ctx: Path) -> None:
    """Populate a temp build context from the installed package."""
    shutil.copytree(
        _PKG_DIR,
        ctx / "checkpoint",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "web", "node_modules"),
    )
    deps = ",\n  ".join(f'"{d}"' for d in _runtime_requirements())
    (ctx / "pyproject.toml").write_text(
        "[project]\n"
        'name = "checkpoint-agents"\n'
        'version = "0.0.0+sidecar"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [\n  {deps}\n]\n\n"
        "[build-system]\n"
        'requires = ["setuptools>=77"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[tool.setuptools.packages.find]\n"
        'include = ["checkpoint*"]\n'
    )


def _build(client, context: Path, tag: str) -> None:
    from docker.errors import APIError, BuildError

    try:
        client.images.build(
            path=str(context),
            dockerfile=_PROXY_DOCKERFILE_REL,
            tag=tag,
            rm=True,
        )
    except (BuildError, APIError) as e:
        raise RuntimeError(
            f"Failed to build sidecar image {tag}: {e}\n"
            f"You can build it manually with `checkpoint docker build-sidecar`."
        ) from e


def ensure_sidecar_image(
    client,
    tag: str = SIDECAR_IMAGE,
    *,
    force: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    """Ensure the sidecar image exists, building it once if needed. Returns the tag."""
    emit = log_fn or log.info
    if not force and sidecar_image_exists(client, tag):
        return tag

    emit(f"Building {tag} (first Docker run only, ~1-2 min)…")

    source_root = _find_source_root()
    if source_root is not None:
        _build(client, source_root, tag)
        return tag

    with tempfile.TemporaryDirectory(prefix="checkpoint-sidecar-ctx-") as tmp:
        ctx = Path(tmp)
        _assemble_wheel_context(ctx)
        _build(client, ctx, tag)
    return tag
