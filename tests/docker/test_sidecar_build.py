"""Unit tests for the sidecar auto-build (no real Docker daemon needed)."""
from __future__ import annotations

from pathlib import Path

import pytest

from checkpoint.docker import sidecar


class _FakeImages:
    def __init__(self, present: bool):
        self._present = present
        self.build_calls: list[dict] = []

    def get(self, tag):
        if self._present:
            return object()
        raise RuntimeError(f"image {tag} not found")

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        return (object(), [])


class _FakeClient:
    def __init__(self, present: bool):
        self.images = _FakeImages(present)


def test_sidecar_image_exists_true_and_false():
    assert sidecar.sidecar_image_exists(_FakeClient(present=True)) is True
    assert sidecar.sidecar_image_exists(_FakeClient(present=False)) is False


def test_ensure_skips_build_when_present():
    client = _FakeClient(present=True)
    tag = sidecar.ensure_sidecar_image(client)
    assert tag == sidecar.SIDECAR_IMAGE
    assert client.images.build_calls == []  # nothing built


def test_ensure_builds_from_source_root_when_absent():
    client = _FakeClient(present=False)
    tag = sidecar.ensure_sidecar_image(client)
    assert tag == sidecar.SIDECAR_IMAGE
    assert len(client.images.build_calls) == 1
    call = client.images.build_calls[0]
    # Source checkout: build straight from the repo root with the proxy Dockerfile.
    assert call["dockerfile"] == "checkpoint/proxy/Dockerfile"
    root = Path(call["path"])
    assert (root / "pyproject.toml").exists()
    assert (root / "checkpoint").is_dir()


def test_ensure_force_rebuilds_even_when_present():
    client = _FakeClient(present=True)
    sidecar.ensure_sidecar_image(client, force=True)
    assert len(client.images.build_calls) == 1


def test_find_source_root_points_at_repo_root():
    root = sidecar._find_source_root()
    assert root is not None
    assert (root / "pyproject.toml").exists()
    assert (root / "checkpoint" / "proxy" / "Dockerfile").exists()


def test_runtime_requirements_nonempty_and_has_mitmproxy():
    reqs = sidecar._runtime_requirements()
    assert reqs, "expected some runtime requirements"
    assert any("mitmproxy" in r for r in reqs)


def test_wheel_context_assembly(monkeypatch, tmp_path):
    """When there's no source checkout, a temp context is assembled with a
    generated pyproject.toml and the copied package."""
    # Force the wheel path and make the (heavy) package copy cheap.
    monkeypatch.setattr(sidecar, "_find_source_root", lambda: None)

    def _fake_copytree(src, dst, ignore=None):
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "__init__.py").write_text("")
        (Path(dst) / "proxy").mkdir()
        (Path(dst) / "proxy" / "Dockerfile").write_text("FROM python:3.11-slim\n")

    monkeypatch.setattr(sidecar.shutil, "copytree", _fake_copytree)

    captured = {}

    def _fake_build(client, context, tag):
        captured["tag"] = tag
        # The generated context must be a valid build context — capture its
        # contents now, before ensure_sidecar_image cleans up the tempdir.
        assert (Path(context) / "checkpoint").is_dir()
        captured["pyproject"] = (Path(context) / "pyproject.toml").read_text()

    monkeypatch.setattr(sidecar, "_build", _fake_build)

    client = _FakeClient(present=False)
    tag = sidecar.ensure_sidecar_image(client)
    assert tag == sidecar.SIDECAR_IMAGE
    assert captured["tag"] == sidecar.SIDECAR_IMAGE
    assert "checkpoint-agents" in captured["pyproject"]
    assert "mitmproxy" in captured["pyproject"]
