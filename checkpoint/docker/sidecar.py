"""Build the TLS-sidecar image on first use.

The docker-mode runner needs a `checkpoint-sidecar:latest` image (a mitmproxy
container that mints a CA at startup and routes intercepted SaaS domains to the
local twins). Historically nothing built it, so the default `checkpoint run`
failed on a clean machine with `ImageNotFound`. `ensure_sidecar_image()` fixes
that: it builds the image once, transparently, the first time it is needed.

The sidecar Dockerfile (`checkpoint/proxy/Dockerfile`) expects a build context
holding `pyproject.toml` + the `checkpoint` package (it runs `pip install .`).
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
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import checkpoint

log = logging.getLogger("checkpoint.docker.sidecar")

# Single source of truth for the sidecar tag (docker/runner.py imports this).
SIDECAR_IMAGE = os.environ.get("CHECKPOINT_SIDECAR_IMAGE", "checkpoint-sidecar:latest")

_PKG_DIR = Path(checkpoint.__file__).resolve().parent          # .../checkpoint
_PROXY_DOCKERFILE_REL = "checkpoint/proxy/Dockerfile"           # relative to a build context root

# Fallback runtime deps, mirroring pyproject.toml, used only when installed
# metadata can't be read (e.g. an odd editable layout).
_FALLBACK_REQUIREMENTS = [
    "fastapi>=0.110", "uvicorn[standard]>=0.27", "httpx>=0.27", "click>=8.1",
    "rich>=13.7", "openai>=1.30", "pydantic>=2.0", "python-dotenv>=1.0",
    "mitmproxy>=10.0", "docker>=7.0", "mcp>=1.27", "sse-starlette>=2.1",
]


def sidecar_image_exists(client, tag: str = SIDECAR_IMAGE) -> bool:
    """True if the image is already present locally."""
    try:
        client.images.get(tag)
        return True
    except Exception:
        return False


def _find_source_root() -> Path | None:
    """Return the repo root if we're running from a source checkout.

    A valid build context has pyproject.toml next to the checkpoint package.
    """
    root = _PKG_DIR.parent
    if (root / "pyproject.toml").exists() and (root / "checkpoint").is_dir():
        return root
    return None


def _runtime_requirements() -> list[str]:
    """Best-effort runtime dependencies for the generated pyproject stub."""
    for dist in ("checkpoint-agents", "checkpoint"):
        try:
            reqs = metadata.requires(dist)
        except metadata.PackageNotFoundError:
            continue
        if not reqs:
            continue
        out: list[str] = []
        for r in reqs:
            # Core deps carry no marker. Of the optional ones, keep exactly the
            # `proxy` extra: mitmproxy is not installed on the host (the addon
            # runs inside this image), but the sidecar itself cannot work
            # without it, so it must land in the generated pyproject.
            if "extra ==" in r:
                if 'extra == "proxy"' not in r.replace("'", '"'):
                    continue
            out.append(r.split(";", 1)[0].strip())
        if out:
            return out
    return list(_FALLBACK_REQUIREMENTS)


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
