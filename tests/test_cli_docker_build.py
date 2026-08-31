"""CLI wrappers for the docker build commands (regression for B1)."""
from __future__ import annotations

import types
from pathlib import Path

from click.testing import CliRunner

import checkpoint.docker.harness_image as harness_image
import checkpoint.docker.sidecar as sidecar
from checkpoint.cli import main


def test_docker_build_passes_three_positional_args(tmp_path, monkeypatch):
    """`checkpoint docker build` must call build_harness_image(dir, entry, tag).

    Before the fix it called `build_harness_image(dir, tag=tag)`, dropping the
    required `harness_entry` positional -> TypeError swallowed into a fake
    'Build failed' and exit 1.
    """
    (tmp_path / "harness.py").write_text("print('hi')\n")
    calls: list[tuple] = []

    def _fake_build(harness_dir, harness_entry, tag):
        calls.append((Path(harness_dir), harness_entry, tag))
        return tag

    monkeypatch.setattr(harness_image, "build_harness_image", _fake_build)

    result = CliRunner().invoke(main, ["docker", "build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    harness_dir, harness_entry, tag = calls[0]
    assert harness_dir == tmp_path
    assert harness_entry is None
    assert tag == "checkpoint-harness:latest"


def test_docker_build_sidecar_success(monkeypatch):
    fake_client = types.SimpleNamespace(ping=lambda: None)
    import docker as docker_pkg

    monkeypatch.setattr(docker_pkg, "from_env", lambda: fake_client)
    seen = {}

    def _fake_ensure(client, force=False, log_fn=None):
        seen["called"] = True
        seen["force"] = force
        return sidecar.SIDECAR_IMAGE

    monkeypatch.setattr(sidecar, "ensure_sidecar_image", _fake_ensure)

    result = CliRunner().invoke(main, ["docker", "build-sidecar"])
    assert result.exit_code == 0, result.output
    assert seen.get("called") is True
    assert "Sidecar image ready" in result.output


def test_docker_build_sidecar_reports_unreachable_daemon(monkeypatch):
    import docker as docker_pkg

    def _boom():
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(docker_pkg, "from_env", _boom)
    result = CliRunner().invoke(main, ["docker", "build-sidecar"])
    assert result.exit_code == 1
    assert "Docker not reachable" in result.output
