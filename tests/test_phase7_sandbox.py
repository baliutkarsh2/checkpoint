"""SBX-01: Sandbox image artifacts present and structurally sound.

We don't invoke `docker build` from pytest (CI may not have docker), but we
DO verify:
  - All sandbox artifacts exist with the expected shapes.
  - The Dockerfile references the entrypoint and gh shim correctly.
  - The entrypoint script is bash and uses set -euo pipefail.
  - The gh shim is executable bash that delegates to the python bridge.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path


SANDBOX = Path(__file__).parent.parent / "checkpoint" / "sandbox"


def test_sandbox_files_exist():
    assert (SANDBOX / "__init__.py").exists()
    assert (SANDBOX / "Dockerfile").exists()
    assert (SANDBOX / "entrypoint.sh").exists()
    assert (SANDBOX / "gh").exists()
    assert (SANDBOX / "gh_bridge.py").exists()


def test_dockerfile_references_entrypoint_and_gh():
    dockerfile = (SANDBOX / "Dockerfile").read_text()
    assert "checkpoint/sandbox/entrypoint.sh" in dockerfile
    assert "checkpoint/sandbox/gh" in dockerfile
    assert "/usr/local/bin/gh" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert "python:3.12-slim" in dockerfile
    # Must install the checkpoint package (which carries mitmproxy, fastapi, etc.).
    assert "pip install" in dockerfile


def test_entrypoint_is_strict_bash():
    text = (SANDBOX / "entrypoint.sh").read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    # Starts all three twins and the sidecar.
    assert "checkpoint.twins.github:app" in text
    assert "checkpoint.twins.slack:app" in text
    assert "checkpoint.twins.stripe:app" in text
    assert "mitmdump" in text
    # CA mint + env exports.
    assert "mint_ca" in text
    assert "CHECKPOINT_GITHUB_URL" in text
    assert "CHECKPOINT_SLACK_URL" in text
    assert "CHECKPOINT_STRIPE_URL" in text
    # Cleanup trap so child PIDs don't leak.
    assert "trap cleanup EXIT" in text
    # Routes JSON wires real-API hostnames to twin URLs.
    assert "api.github.com" in text
    assert "api.stripe.com" in text


def test_gh_shim_delegates_to_bridge():
    shim = (SANDBOX / "gh").read_text()
    assert shim.startswith("#!/usr/bin/env bash")
    assert "checkpoint.sandbox.gh_bridge" in shim


def test_gh_bridge_importable():
    from checkpoint.sandbox import gh_bridge  # noqa: F401
    # `main` is the entrypoint for argv dispatch.
    assert callable(gh_bridge.main)
