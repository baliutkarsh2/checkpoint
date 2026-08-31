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
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import APIError, NotFound

from ..proxy.routes import all_domains, lookup, register
from ..runner import (
    RunResult,
    _evaluate,
    _extract_final_answer,
)
from ..scenario import Scenario
from .harness_image import HarnessImageError, build_harness_image
from .sidecar import SIDECAR_IMAGE, ensure_sidecar_image

_DOCKER_TWIN_APPS = {
    "github": "checkpoint.twins.github:app",
    "slack": "checkpoint.twins.slack:app",
    "stripe": "checkpoint.twins.stripe:app",
    "linear": "checkpoint.twins.linear:app",
    "supabase": "checkpoint.twins.supabase:app",
    "discord": "checkpoint.twins.discord:app",
    "google-workspace": "checkpoint.twins.google_workspace:app",
}

# Maps clone name -> the domain(s) the harness's SDK will call.
# These are registered in CHECKPOINT_ROUTES so the sidecar addon can intercept.
_CLONE_DOMAINS = {
    "github": ["api.github.com"],
    "slack": ["slack.com"],
    "stripe": ["api.stripe.com"],
    "linear": ["api.linear.app"],
    "supabase": ["supabase.co", "checkpoint.supabase.co"],
    "discord": ["discord.com"],
    "google-workspace": ["gmail.googleapis.com", "www.googleapis.com"],
}

# The first twin in the list gets this port; each subsequent twin increments by 1.
_BASE_TWIN_PORT = 18080


log = logging.getLogger("checkpoint.docker.runner")


@dataclass
class DockerRunResult(RunResult):
    metrics: dict | None = None
    agent_trace: dict | None = None


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


def _apply_seed_in_container(sidecar, twin_port: int, seed_name: str) -> None:
    """POST /_seed/<name> to a twin running in the sidecar's shared netns."""
    cmd = [
        "python", "-c",
        (
            f"import urllib.request; "
            f"req = urllib.request.Request('http://127.0.0.1:{twin_port}/_seed/{seed_name}', method='POST', data=b''); "
            f"urllib.request.urlopen(req, timeout=10)"
        ),
    ]
    try:
        sidecar.exec_run(cmd)
    except Exception as e:
        log.warning("seed %r on :%d failed: %s", seed_name, twin_port, e)


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


def _read_output(archal_out: Path, name: str) -> dict | None:
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
    cwd: str | None = None,
    judge_model: str = "gpt-4o-mini",
    verbose: bool = False,
) -> DockerRunResult:
    clones = scenario.clones or ["github"]
    unknown = [c for c in clones if c not in _DOCKER_TWIN_APPS]
    if unknown:
        return DockerRunResult("", "", -1, [], {}, error=f"Docker runner: unknown clone(s): {unknown}")

    client = docker.from_env()
    try:
        client.ping()
    except Exception as e:
        return DockerRunResult("", "", -1, [], {}, error=f"Docker daemon not reachable: {e}")

    archal_out = Path(tempfile.mkdtemp(prefix="checkpoint-archal-out-"))
    run_id = uuid.uuid4().hex[:8]
    network_name = f"checkpoint-net-{run_id}"
    sidecar_name = f"checkpoint-sidecar-{run_id}"
    harness_tag = f"checkpoint-harness:run-{run_id}"

    # Assign one port per clone, all sharing the sidecar's netns.
    # Ports start at _BASE_TWIN_PORT and increment so they never collide.
    clone_ports: dict[str, int] = {
        clone: _BASE_TWIN_PORT + i for i, clone in enumerate(clones)
    }

    # Build the CHECKPOINT_ROUTES mapping for the sidecar addon.
    # Each domain for a clone maps to http://127.0.0.1:<port> in the shared netns.
    routes: dict[str, str] = {}
    for clone, port in clone_ports.items():
        twin_url = f"http://127.0.0.1:{port}"
        for domain in _CLONE_DOMAINS.get(clone, []):
            routes[domain] = twin_url
            # For subdomains not in _ROUTES, inherit the parent domain's bootstrap token.
            token: str | None = None
            parts = domain.split(".")
            for i in range(len(parts)):
                parent_route = lookup(".".join(parts[i:]))
                if parent_route is not None:
                    token = parent_route.bootstrap_token
                    break
            register(domain, twin_url, bootstrap_token=token)

    # Default reverse-mode upstream = first clone (fallback if addon has no route).
    first_twin_url = f"http://127.0.0.1:{clone_ports[clones[0]]}"

    sidecar = None
    twin_containers: list = []  # list of (clone, port, container)
    harness = None
    network = None

    try:
        _write_hosts_file(archal_out)

        # Build the TLS sidecar image on first use so a clean machine works
        # out of the box (no manual `docker build` step).
        try:
            ensure_sidecar_image(client, log_fn=lambda m: sys.stderr.write(m + "\n"))
        except Exception as e:
            return DockerRunResult("", "", -1, [], {}, error=f"Sidecar image build failed: {e}")

        try:
            build_harness_image(harness_dir, " ".join(harness_cmd), harness_tag)
        except HarnessImageError as e:
            return DockerRunResult("", "", -1, [], {}, error=f"Harness image build failed: {e}")

        network = client.networks.create(network_name, driver="bridge")

        sidecar = client.containers.run(
            SIDECAR_IMAGE,
            detach=True,
            name=sidecar_name,
            network=network_name,
            volumes={str(archal_out): {"bind": "/archal-out", "mode": "rw"}},
            environment={
                "SIDECAR_PORT": "443",
                "TWIN_UPSTREAM": first_twin_url,
                "CHECKPOINT_ROUTES": json.dumps(routes),
            },
        )

        if not _wait_for_ca(archal_out):
            return DockerRunResult("", "", -1, [], {}, error="Sidecar did not mint CA within 10s")

        if not _wait_for_sidecar_listening(sidecar):
            return DockerRunResult("", "", -1, [], {}, error="Sidecar mitmdump did not start listening within 15s")

        # Start one twin container per clone, all sharing the sidecar's netns so
        # mitmproxy can reach them at 127.0.0.1:<port> in the shared namespace.
        for clone, port in clone_ports.items():
            twin_app = _DOCKER_TWIN_APPS[clone]
            twin_ctr = client.containers.run(
                SIDECAR_IMAGE,
                detach=True,
                name=f"checkpoint-twin-{clone}-{run_id}",
                network_mode=f"container:{sidecar.id}",
                entrypoint=[
                    "python", "-m", "uvicorn", twin_app,
                    "--host", "127.0.0.1", "--port", str(port),
                    "--log-level", "warning",
                ],
            )
            twin_containers.append((clone, port, twin_ctr))

        # Wait for every twin to be healthy inside the shared netns.
        for clone, port, _ in twin_containers:
            if not _wait_for_twin_in_container(sidecar, port):
                return DockerRunResult(
                    "", "", -1, [], {},
                    error=f"Twin '{clone}' failed to start on 127.0.0.1:{port} in shared netns",
                )

        # Apply seeds via /_seed/<name> inside the shared netns.
        from ..runner import _parse_seed_spec
        seed_map = _parse_seed_spec(scenario.config.get("seed") or scenario.config.get("seed_name"), clones)
        for clone, port, _ in twin_containers:
            seed_name = seed_map.get(clone)
            if seed_name:
                _apply_seed_in_container(sidecar, port, seed_name)

        # Resolve sidecar IP so the harness can reach :443 via extra_hosts.
        sidecar.reload()
        sidecar_ip = sidecar.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        if not sidecar_ip:
            return DockerRunResult("", "", -1, [], {}, error="Could not resolve sidecar IP on user network")

        env = _build_env(scenario, judge_model)
        # Inject per-clone bootstrap tokens so the harness SDKs authenticate.
        from ..runner import _CLONE_BOOTSTRAP_TOKEN_ENV
        for clone in clones:
            tok = _CLONE_BOOTSTRAP_TOKEN_ENV.get(clone)
            if tok:
                env[tok[0]] = tok[1]

        harness = client.containers.run(
            harness_tag,
            detach=True,
            network=network_name,
            volumes={str(archal_out): {"bind": "/archal-out", "mode": "rw"}},
            environment=env,
            # Point every service domain -> sidecar so the harness's SDK calls land on :443.
            extra_hosts={domain: sidecar_ip for domain in all_domains()},
        )

        # Stream logs (when --docker-logs) on a daemon thread so following the
        # log never blocks the timeout: the main thread always waits at most
        # scenario.timeout for the harness to exit, then kills a hung agent.
        if verbose:
            def _pump_logs() -> None:
                try:
                    for _line in harness.logs(stream=True, follow=True):
                        sys.stderr.write(_line.decode("utf-8", errors="replace"))
                except Exception:
                    pass

            threading.Thread(target=_pump_logs, daemon=True).start()

        try:
            exit_info = harness.wait(timeout=scenario.timeout)
        except Exception as e:
            try:
                harness.kill()
            except Exception:
                pass
            return DockerRunResult(
                "", "", -1, [], {},
                error=f"Harness timed out after {scenario.timeout}s (or wait failed: {e})",
            )

        stdout = harness.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = harness.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")[-4000:]
        exit_code = int(exit_info.get("StatusCode", -1))

        metrics = _read_output(archal_out, "metrics.json")
        agent_trace = _read_output(archal_out, "agent-trace.json")

        # Merge state and trace from all twins.
        per_clone_state = {clone: _fetch_state_in_container(sidecar, port)
                           for clone, port, _ in twin_containers}
        per_clone_trace = {clone: _fetch_trace_in_container(sidecar, port)
                           for clone, port, _ in twin_containers}
        from ..runner import _merge_state_for_clones, _merge_trace_for_clones
        merged_state = _merge_state_for_clones(per_clone_state)
        merged_trace = _merge_trace_for_clones(per_clone_trace)

        result = DockerRunResult(
            final_answer=_extract_final_answer(stdout),
            stderr=stderr,
            exit_code=exit_code,
            trace=merged_trace,
            state=merged_state,
            stdout=stdout,
            metrics=metrics,
            agent_trace=agent_trace,
        )

        if exit_code != 0:
            result.error = f"Harness exited {exit_code}"
            return result

        _evaluate(scenario, result, judge_model)
        return result

    finally:
        # Teardown: harness first, then all twin containers, then sidecar.
        containers_to_stop = (
            [(harness, "harness")]
            + [(ctr, f"twin-{clone}") for clone, _, ctr in twin_containers]
            + [(sidecar, "sidecar")]
        )
        for c, _name in containers_to_stop:
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
