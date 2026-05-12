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

import sys

from ..runner import (
    RunResult,
    _evaluate,
    _extract_final_answer,
    _fetch_state,
    _fetch_trace,
    _free_port,
    _wait_healthy,
)
from ..scenario import Scenario
from ..proxy.routes import register, all_domains
from .harness_image import build_harness_image, HarnessImageError


def _start_twin_for_docker(clone: str, port: int) -> subprocess.Popen:
    """Start the twin bound to 0.0.0.0 so docker containers can reach it via
    host.docker.internal:<port>. The v0 _start_twin binds to 127.0.0.1, which
    is correct for local-mode but unreachable from docker bridge."""
    if clone != "github":
        raise ValueError(f"docker runner only supports clone=github (got {clone!r})")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.github:app",
            "--host", "0.0.0.0",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

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


def _extra_hosts() -> dict:
    """Build docker extra_hosts dict for sidecar+harness.

    Docker manages /etc/hosts at container start and inserts these entries
    after the default localhost lines. We add:
      - host.docker.internal -> host-gateway (for sidecar to reach the in-process twin)
      - <registered domain> -> 127.0.0.1 (for harness DNS hijack to sidecar)
    """
    hosts: dict = {"host.docker.internal": "host-gateway"}
    for domain in all_domains():
        hosts[domain] = "127.0.0.1"
    return hosts


def _wait_for_ca(archal_out: Path, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    cert = archal_out / "ca.crt"
    while time.time() < deadline:
        if cert.exists() and cert.stat().st_size > 0:
            return True
        time.sleep(0.2)
    return False


def _wait_for_twin_in_container(sidecar, twin_port: int, timeout: float = 15.0) -> bool:
    """Wait until the twin (in the sidecar's netns) responds on /_health."""
    deadline = time.time() + timeout
    cmd = [
        "python", "-c",
        f"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:{twin_port}/_health', timeout=1).status==200 else 1)"
    ]
    while time.time() < deadline:
        try:
            ec, _ = sidecar.exec_run(cmd, demux=False)
            if ec == 0:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _fetch_trace_in_container(sidecar, twin_port: int) -> list:
    cmd = ["python", "-c",
           f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{twin_port}/_trace', timeout=5).read().decode())"]
    try:
        ec, out = sidecar.exec_run(cmd)
        if ec == 0:
            return json.loads(out.decode("utf-8", errors="replace"))
    except Exception:
        pass
    return []


def _fetch_state_in_container(sidecar, twin_port: int) -> dict:
    cmd = ["python", "-c",
           f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{twin_port}/_state', timeout=5).read().decode())"]
    try:
        ec, out = sidecar.exec_run(cmd)
        if ec == 0:
            return json.loads(out.decode("utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def _wait_for_sidecar_listening(sidecar, port: int = 443, timeout: float = 15.0) -> bool:
    """Wait until mitmdump in the sidecar container is actually listening on :port.

    The CA file is minted BEFORE mitmdump starts (see entrypoint.sh), so the
    presence of ca.crt is not sufficient. We grep the container logs for
    mitmproxy's "listening at" announcement.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            logs = sidecar.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            if "listening at" in logs.lower() or "Proxy listening" in logs:
                # Give mitmproxy a beat to actually open the socket.
                time.sleep(0.3)
                return True
        except Exception:
            pass
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

    # Twin port is the one mitmdump's addon forwards to from inside the sidecar
    # container. Since the sidecar and twin will share a netns, the twin can be
    # bound to 127.0.0.1 inside the netns and reached by the sidecar via the
    # same 127.0.0.1. We pick a high port that doesn't clash with mitmdump (443).
    twin_port = 18080
    twin = None  # twin runs in a container, started after the sidecar (shared netns)
    archal_out = Path(tempfile.mkdtemp(prefix="checkpoint-archal-out-"))
    sidecar = None
    harness = None
    network = None  # user-defined bridge network so containers can resolve each other by name
    run_id = uuid.uuid4().hex[:8]
    network_name = f"checkpoint-net-{run_id}"
    sidecar_name = f"checkpoint-sidecar-{run_id}"
    twin_name = f"checkpoint-twin-{run_id}"
    harness_tag = f"checkpoint-harness:run-{run_id}"

    try:
        # Sidecar forwards to the twin on 127.0.0.1:<twin_port> in the shared netns.
        twin_url_from_container = f"http://127.0.0.1:{twin_port}"
        register("api.github.com", twin_url_from_container)

        # _write_hosts_file is kept for unit-test compatibility but we no longer
        # mount it into containers — docker's native extra_hosts is the right
        # mechanism. Bind-mounting our own /etc/hosts erases the host-gateway
        # entry that --add-host inserts, breaking sidecar -> twin connectivity.
        _write_hosts_file(archal_out)

        try:
            build_harness_image(harness_dir, " ".join(harness_cmd), harness_tag)
        except HarnessImageError as e:
            return DockerRunResult("", "", -1, [], {}, error=f"Harness image build failed: {e}")

        # The sidecar runs mitmproxy in a separate Python process; its routes
        # registry is independent of ours. Pass twin URLs via CHECKPOINT_ROUTES
        # JSON env so the addon's _seed_routes_from_env() can register() them.
        routes_env = json.dumps({"api.github.com": twin_url_from_container})

        # Create an isolated user-defined bridge network for this run.
        # The harness then resolves "api.github.com" -> sidecar's IP via extra_hosts.
        network = client.networks.create(network_name, driver="bridge")

        sidecar = client.containers.run(
            SIDECAR_IMAGE,
            detach=True,
            name=sidecar_name,
            network=network_name,
            volumes={
                str(archal_out): {"bind": "/archal-out", "mode": "rw"},
            },
            environment={
                "SIDECAR_PORT": "443",
                "TWIN_UPSTREAM": twin_url_from_container,
                "CHECKPOINT_ROUTES": routes_env,
            },
            # IMPORTANT: do NOT inject api.github.com -> 127.0.0.1 into the
            # SIDECAR's /etc/hosts — mitmproxy's reverse-mode upstream resolution
            # would loop back to itself. The harness gets the hijack separately.
        )

        if not _wait_for_ca(archal_out):
            return DockerRunResult(
                "", "", -1, _fetch_trace_in_container(sidecar, twin_port), _fetch_state_in_container(sidecar, twin_port),
                error="Sidecar did not mint CA within 10s",
            )

        if not _wait_for_sidecar_listening(sidecar):
            return DockerRunResult(
                "", "", -1, _fetch_trace_in_container(sidecar, twin_port), _fetch_state_in_container(sidecar, twin_port),
                error="Sidecar mitmdump did not start listening within 15s",
            )

        # Start the GitHub twin as a sidecar-netns-sharing container so that
        # mitmproxy can reach it at 127.0.0.1:<twin_port> in the shared netns.
        twin = client.containers.run(
            SIDECAR_IMAGE,
            detach=True,
            name=twin_name,
            network_mode=f"container:{sidecar.id}",
            entrypoint=[
                "python", "-m", "uvicorn", "checkpoint.twins.github:app",
                "--host", "127.0.0.1", "--port", str(twin_port),
                "--log-level", "warning",
            ],
        )

        if not _wait_for_twin_in_container(sidecar, twin_port):
            return DockerRunResult(
                "", "", -1, [], {},
                error=f"Twin failed to start on 127.0.0.1:{twin_port} in shared netns",
            )

        # Resolve sidecar IP on the user-defined network so the harness's
        # /etc/hosts can map api.github.com -> sidecar_ip.
        sidecar.reload()
        sidecar_ip = sidecar.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        if not sidecar_ip:
            return DockerRunResult("", "", -1, [], {},
                                   error="Could not resolve sidecar IP on user network")

        env = _build_env(scenario, judge_model)
        # Harness gets its own netns on the same user-defined network. We hijack
        # api.github.com -> sidecar_ip via extra_hosts so the harness's stock
        # https://api.github.com calls land on the sidecar's :443.
        harness_extra_hosts = {domain: sidecar_ip for domain in all_domains()}
        harness = client.containers.run(
            harness_tag,
            detach=True,
            network=network_name,
            volumes={
                str(archal_out): {"bind": "/archal-out", "mode": "rw"},
            },
            environment=env,
            extra_hosts=harness_extra_hosts,
        )

        try:
            exit_info = harness.wait(timeout=scenario.timeout)
        except Exception as e:
            try:
                harness.kill()
            except Exception:
                pass
            return DockerRunResult(
                "", "", -1, _fetch_trace_in_container(sidecar, twin_port), _fetch_state_in_container(sidecar, twin_port),
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
            trace=_fetch_trace_in_container(sidecar, twin_port),
            state=_fetch_state_in_container(sidecar, twin_port),
            metrics=metrics,
            agent_trace=agent_trace,
        )

        if exit_code != 0:
            result.error = f"Harness exited {exit_code}"
            return result

        _evaluate(scenario, result, judge_model)
        return result

    finally:
        # Teardown — never raise out of finally. Stop harness first (depends on
        # sidecar), then twin (depends on sidecar netns), then sidecar.
        for c, _name in [(harness, "harness"), (twin, "twin"), (sidecar, "sidecar")]:
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
        if network is not None:
            try:
                network.remove()
            except Exception:
                pass
        shutil.rmtree(archal_out, ignore_errors=True)
        try:
            client.images.remove(harness_tag, force=True)
        except Exception:
            pass
