"""CLI-03: `checkpoint doctor` environment checks.

A flat pipeline of "is this thing in working order?" checks. Each returns a
`Check` row that the CLI renders as a table with copy-paste fixes on failure.

The function ``run_checks()`` is the single public entrypoint; tests call it
directly. The CLI command in ``cli.py`` consumes the same list.
"""
from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


# No ports are checked by default. Subprocess-mode twins bind dynamic free
# ports, docker-mode twins live inside the sidecar's network namespace (ports
# 18080+, not host-bindable), and the dashboard's 4001 is only in use while
# `checkpoint serve` runs — so a fixed port probe here is pure noise and used
# to fail `doctor` for reasons unrelated to Checkpoint. Callers (and tests) can
# still pass an explicit `ports=(...)` to probe a specific port.
DEFAULT_PORTS: tuple[int, ...] = ()


def _check_python_version() -> Check:
    ok = sys.version_info >= (3, 11)
    return Check(
        name="Python >= 3.11",
        ok=ok,
        detail=f"{sys.version.split()[0]}",
        fix=None if ok else "Install Python 3.11+ (e.g. `brew install python@3.12`).",
    )


def _check_docker() -> Check:
    try:
        import docker  # type: ignore
    except Exception as e:
        return Check(
            name="docker SDK importable",
            ok=False,
            detail=f"import failed: {e}",
            fix="pip install docker",
        )
    try:
        client = docker.from_env()
        client.ping()
        version = client.version().get("Version", "?")
        return Check(
            name="Docker daemon reachable",
            ok=True,
            detail=f"Docker {version}",
            fix=None,
        )
    except Exception as e:
        return Check(
            name="Docker daemon reachable",
            ok=False,
            detail=str(e)[:160],
            fix="Start Docker Desktop, or on Linux: `sudo systemctl start docker` "
                "(then re-login or `sg docker -c '...'`).",
        )


def _check_sidecar_image() -> Check:
    """Informational: is the TLS sidecar image built yet?

    Never fails `doctor` — an absent image is fine because the docker runner
    auto-builds it on first use. We only surface the state so users aren't
    surprised by a one-time ~1-2 min build.
    """
    name = "TLS sidecar image"
    try:
        import docker  # type: ignore

        from .docker.sidecar import SIDECAR_IMAGE, sidecar_image_exists

        client = docker.from_env()
        client.ping()
    except Exception:
        return Check(
            name=name,
            ok=True,
            detail="skipped (docker not reachable; only needed for docker mode)",
        )
    if sidecar_image_exists(client, SIDECAR_IMAGE):
        return Check(name=name, ok=True, detail=f"{SIDECAR_IMAGE} present")
    return Check(
        name=name,
        ok=True,
        detail="absent — builds automatically on the first `checkpoint run` "
               "(or run `checkpoint docker build-sidecar`)",
    )


def _check_port_free(port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return Check(
            name=f"Port {port} free",
            ok=True,
            detail="bind OK on 127.0.0.1",
        )
    except OSError as e:
        return Check(
            name=f"Port {port} free",
            ok=False,
            detail=str(e),
            fix=f"Stop the process using port {port} "
                f"(`lsof -ti :{port} | xargs kill`).",
        )
    finally:
        sock.close()


def _check_openai_key() -> Check:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    ok = bool(key)
    return Check(
        name="OPENAI_API_KEY set",
        ok=ok,
        detail="present" if ok else "missing",
        fix=None if ok else "export OPENAI_API_KEY=sk-... "
                            "(or put it in a .env file in your project directory).",
    )


def _check_mitmproxy() -> Check:
    try:
        import mitmproxy  # type: ignore  # noqa: F401
        return Check(
            name="mitmproxy importable",
            ok=True,
            detail="OK",
        )
    except Exception as e:
        return Check(
            name="mitmproxy importable",
            ok=False,
            detail=f"import failed: {e}",
            fix='pip install "checkpoint-agents[proxy]"',
        )


def _check_checkpoint_config(cwd: Path | None = None) -> Check:
    cwd = cwd or Path.cwd()
    path = cwd / ".checkpoint.json"
    if path.exists():
        return Check(
            name=".checkpoint.json present",
            ok=True,
            detail=str(path),
        )
    # Informational only — not a failure. CLI treats this as a warning,
    # never a non-zero exit.
    return Check(
        name=".checkpoint.json present",
        ok=True,
        detail="absent (informational; checkpoint run --harness works without it)",
    )


def run_checks(
    *,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    cwd: Path | None = None,
) -> list[Check]:
    """Run the full diagnostic pipeline and return the rows in order.

    Pure function — no I/O beyond the network/disk probes each check
    performs. Order is stable so CLI output is deterministic across runs.
    """
    checks: list[Check] = []
    checks.append(_check_python_version())
    checks.append(_check_docker())
    checks.append(_check_sidecar_image())
    for p in ports:
        checks.append(_check_port_free(p))
    checks.append(_check_openai_key())
    checks.append(_check_mitmproxy())
    checks.append(_check_checkpoint_config(cwd))
    return checks


def all_passed(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)
