"""``python -m checkpoint.proxy``: the sidecar entrypoint's contract with the Docker runner."""
from __future__ import annotations

import re
import signal
import socket
import ssl
import subprocess
import sys

import httpx
import pytest

from checkpoint.proxy.__main__ import main
from checkpoint.proxy.server import Route, routes_to_json

from .support import https_on, read_head


@pytest.fixture
def sidecar(tmp_path, echo_upstream):
    routes = routes_to_json([Route("api.example.test", echo_upstream, auth_header="token twin")])
    proc = subprocess.Popen(
        [sys.executable, "-m", "checkpoint.proxy", "--listen", "127.0.0.1:0",
         "--transparent-port", "0", "--ca-dir", str(tmp_path), "--routes", routes,
         "--allow", "only.example"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        yield proc, tmp_path
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def test_prints_ready_once_listening_and_serves_both_modes(sidecar):
    proc, ca_dir = sidecar
    assert proc.stdout.readline().strip() == "ready"
    banner = proc.stderr.readline()
    port, transparent = map(int, re.search(r":(\d+), transparent TLS on :(\d+)", banner).groups())
    assert (ca_dir / "ca.crt").is_file() and (ca_dir / "bundle.pem").is_file()
    trust = ssl.create_default_context(cafile=str(ca_dir / "ca.crt"))

    with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=trust, trust_env=False) as client:
        echo = client.get("https://api.example.test/echo/cli").json()
    assert ["authorization", "token twin"] in echo["headers"]

    sock = socket.create_connection(("127.0.0.1", transparent), timeout=10)
    conn = https_on(trust.wrap_socket(sock, server_hostname="api.example.test"), "api.example.test")
    conn.request("GET", "/echo/transparent")
    assert conn.getresponse().status == 200
    conn.close()

    blocked = socket.create_connection(("127.0.0.1", port), timeout=10)
    blocked.sendall(b"CONNECT elsewhere.example:443 HTTP/1.1\r\nHost: elsewhere.example\r\n\r\n")
    assert read_head(blocked).startswith(b"HTTP/1.1 403")  # --allow switched to an allowlist
    blocked.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_shuts_down_cleanly(sidecar):
    """As PID 1 in the sidecar, ignoring SIGTERM would stall every `docker stop`."""
    proc, _ = sidecar
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=5) == 0


@pytest.mark.parametrize("args", [
    ["--routes", "[1]"],
    ["--routes", '{"api.github.com": "ftp://nope"}'],
    ["--listen", "no-port"],
])
def test_invalid_arguments_exit_with_usage_error(tmp_path, args):
    with pytest.raises(SystemExit) as exc:
        main(["--ca-dir", str(tmp_path), *args])
    assert exc.value.code == 2
