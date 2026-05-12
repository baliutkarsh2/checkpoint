"""Docker-mode runner: orchestrate twin + TLS sidecar + harness container.

Mirrors checkpoint.runner.run_once's interface. The CLI dispatches here when
--docker is set.

Wiring (one run):
  1. Pick a free port on the host; start the GitHub twin in-process on it.
  2. Wait for /_health.
  3. Register routes: "api.github.com" -> "http://host.docker.internal:<twin_port>".
  4. Build sidecar image if not present; build harness image (via build_harness_image).
  5. Create a host tempdir to use as /archal-out (shared volume).
  6. Write a tmp /etc/hosts file: "127.0.0.1 api.github.com".
  7. Start sidecar container with /archal-out + /etc/hosts mounted, exposing :443
     inside its netns, with --add-host host.docker.internal:host-gateway.
  8. Wait for /archal-out/ca.crt to appear (sidecar's entrypoint minted it).
  9. Start harness container with network_mode=container:<sidecar>, same /archal-out
     + /etc/hosts mounted, full env-var matrix injected.
  10. Wait for harness to exit (or timeout).
  11. Read /archal-out/metrics.json and /archal-out/agent-trace.json if present.
  12. Fetch trace + state from twin via http://127.0.0.1:<twin_port>/_trace and /_state.
  13. Evaluate criteria with v0's _evaluate.
  14. Tear down everything (harness, sidecar, twin, tmpdir) — even on error.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import docker
from docker.errors import APIError, NotFound

from ..runner import (
    RunResult,
    _evaluate,
    _extract_final_answer,
    _fetch_state,
    _fetch_trace,
    _free_port,
    _start_twin,
    _wait_healthy,
)
from ..scenario import Scenario
from ..proxy.routes import register, all_domains
from .harness_image import build_harness_image, HarnessImageError

log = logging.getLogger("checkpoint.docker.runner")

SIDECAR_IMAGE = os.environ.get("CHECKPOINT_SIDECAR_IMAGE", "checkpoint-sidecar:latest")


@dataclass
class DockerRunResult(RunResult):
    metrics: Optional[dict] = None
    agent_trace: Optional[dict] = None


def _write_hosts_file(tmpdir: Path) -> Path:
    # Mapping for the harness to resolve real domains -> sidecar (which shares the netns).
    path = tmpdir / "etc-hosts"
    lines = [
        "127.0.0.1 localhost",
        "::1 localhost",
    ]
    for domain in all_domains():
        lines.append(f"127.0.0.1 {domain}")
    path.write_text("\n".join(lines) + "\n")
    return path


def _wait_for_ca(archal_out: Path, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    cert = archal_out / "ca.crt"
    while time.time() < deadline:
        if cert.exists() and cert.stat().st_size > 0:
            return True
        time.sleep(0.2)
    return False


def _build_env(scenario: Scenario, judge_model: str) -> dict:
    env: dict = {
        # Archal canonical vars (SCOPE §6.1)
        "ARCHAL_ENGINE_TASK": scenario.prompt,
        "ARCHAL_ENGINE_MODE": "docker",
        "ARCHAL_METRICS_FILE": "/archal-out/metrics.json",
        "ARCHAL_AGENT_TRACE_FILE": "/archal-out/agent-trace.json",
        "ARCHAL_OUT_DIR": "/archal-out",
        # CA env vars (SCOPE §2.3 / §6.1)
        "NODE_EXTRA_CA_CERTS": "/archal-out/ca.crt",
        "SSL_CERT_FILE": "/archal-out/ca.crt",
        "REQUESTS_CA_BUNDLE": "/archal-out/ca.crt",
        "CURL_CA_BUNDLE": "/archal-out/ca.crt",
        # Checkpoint aliases (so v0 example/harness.py-style harnesses still work)
        "CHECKPOINT_TASK": scenario.prompt,
        "CHECKPOINT_MODE": "docker",
    }
    # Model is optional (SCOPE §6.2)
    if judge_model:
        env["ARCHAL_ENGINE_MODEL"] = judge_model
    # Forward provider API keys if present (SCOPE §6.2)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    return env


def _read_output(archal_out: Path, name: str) -> Optional[dict]:
    p = Path(archal_out) / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("malformed %s in /archal-out; ignoring", name)
        return None


def docker_run_once(
    scenario: Scenario,
    harness_cmd: list,
    harness_dir: Path,
    cwd: Optional[str] = None,
    judge_model: str = "gpt-4o-mini",
) -> DockerRunResult:
    clones = scenario.clones or ["github"]
    if len(clones) > 1:
        return DockerRunResult("", "", -1, [], {}, error=f"Multi-clone not supported in Phase 1: {clones}")
    clone = clones[0]
    if clone != "github":
        return DockerRunResult("", "", -1, [], {}, error=f"Phase 1 only supports clone=github (got {clone!r})")

    client = docker.from_env()
    try:
        client.ping()
    except Exception as e:
        return DockerRunResult("", "", -1, [], {}, error=f"Docker daemon not reachable: {e}")

    twin_port = _free_port()
    twin = _start_twin(clone, twin_port)
    archal_out = Path(tempfile.mkdtemp(prefix="checkpoint-archal-out-"))
    sidecar = None
    harness = None
    run_id = uuid.uuid4().hex[:8]
    sidecar_name = f"checkpoint-sidecar-{run_id}"
    harness_tag = f"checkpoint-harness:run-{run_id}"

    try:
        if not _wait_healthy(twin_port):
            return DockerRunResult("", "", -1, [], {}, error="Twin failed to start")

        # Sidecar talks to host's twin via host.docker.internal.
        twin_url_from_container = f"http://host.docker.internal:{twin_port}"
        register("api.github.com", twin_url_from_container)

        hosts_file = _write_hosts_file(archal_out)

        try:
            build_harness_image(harness_dir, " ".join(harness_cmd), harness_tag)
        except HarnessImageError as e:
            return DockerRunResult("", "", -1, [], {}, error=f"Harness image build failed: {e}")

        sidecar = client.containers.run(
            SIDECAR_IMAGE,
            detach=True,
            name=sidecar_name,
            volumes={
                str(archal_out): {"bind": "/archal-out", "mode": "rw"},
                str(hosts_file): {"bind": "/etc/hosts", "mode": "ro"},
            },
            environment={"SIDECAR_PORT": "443"},
            extra_hosts={"host.docker.internal": "host-gateway"},
        )

        if not _wait_for_ca(archal_out):
            return DockerRunResult(
                "", "", -1, _fetch_trace(twin_port), _fetch_state(twin_port),
                error="Sidecar did not mint CA within 10s",
            )

        env = _build_env(scenario, judge_model)
        harness = client.containers.run(
            harness_tag,
            detach=True,
            network_mode=f"container:{sidecar.id}",
            volumes={
                str(archal_out): {"bind": "/archal-out", "mode": "rw"},
                str(hosts_file): {"bind": "/etc/hosts", "mode": "ro"},
            },
            environment=env,
        )

        try:
            exit_info = harness.wait(timeout=scenario.timeout)
        except Exception as e:
            try:
                harness.kill()
            except Exception:
                pass
            return DockerRunResult(
                "", "", -1, _fetch_trace(twin_port), _fetch_state(twin_port),
                error=f"Harness wait failed: {e}",
            )

        stdout = harness.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = harness.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")[-4000:]
        exit_code = int(exit_info.get("StatusCode", -1))

        metrics = _read_output(archal_out, "metrics.json")
        agent_trace = _read_output(archal_out, "agent-trace.json")

        result = DockerRunResult(
            final_answer=_extract_final_answer(stdout),
            stderr=stderr,
            exit_code=exit_code,
            trace=_fetch_trace(twin_port),
            state=_fetch_state(twin_port),
            metrics=metrics,
            agent_trace=agent_trace,
        )

        if exit_code != 0:
            result.error = f"Harness exited {exit_code}"
            return result

        _evaluate(scenario, result, judge_model)
        return result

    finally:
        # Teardown — never raise out of finally.
        for c, _name in [(harness, "harness"), (sidecar, "sidecar")]:
            if c is None:
                continue
            try:
                c.stop(timeout=3)
            except (NotFound, APIError):
                pass
            except Exception:
                pass
            try:
                c.remove(force=True)
            except (NotFound, APIError):
                pass
            except Exception:
                pass
        twin.terminate()
        try:
            twin.wait(timeout=3)
        except subprocess.TimeoutExpired:
            twin.kill()
        shutil.rmtree(archal_out, ignore_errors=True)
        try:
            client.images.remove(harness_tag, force=True)
        except Exception:
            pass
