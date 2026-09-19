"""Fixtures for the intercept-proxy tests. Everything is local; nothing touches the internet.

* ``github_twin`` — the real GitHub twin under uvicorn in a subprocess.
* ``echo_upstream`` — an in-thread app whose responses show exactly what the
  proxy forwarded, plus streaming/HEAD/204/slow endpoints (tests/proxy/support.py).
* ``tls_echo`` — a raw TLS echo server with its own self-signed certificate,
  used to prove that allowed-but-unrouted traffic is tunnelled, not decrypted.
* ``make_proxy`` — starts InterceptProxy instances and always stops them.

The upstreams are package-scoped, not session-scoped: they are worth sharing
across these modules, but a twin subprocess and two idle servers left running
for the remainder of a full pytest session cost the later tests real time
(tests/test_phase8_performance.py measures a cold start against a budget).
"""
from __future__ import annotations

import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from checkpoint.proxy.ca import CertificateAuthority
from checkpoint.proxy.server import EgressPolicy, InterceptProxy, Route

from .support import ECHO_APP, ThreadedUvicorn, TLSEcho, free_port, wait_for


@pytest.fixture(scope="package")
def echo_upstream() -> Iterator[str]:
    with ThreadedUvicorn(ECHO_APP) as server:
        yield f"http://127.0.0.1:{server.port}"


@pytest.fixture(scope="package")
def github_twin() -> Iterator[str]:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "checkpoint.twins.github:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"

    def healthy() -> bool:
        try:
            return httpx.get(f"{url}/_health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        wait_for(healthy, timeout=30)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="package")
def tls_echo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TLSEcho]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls-echo")
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve_one(conn: socket.socket) -> None:
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                while data := tls.recv(65536):
                    tls.sendall(data)
        except OSError:
            pass

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=serve_one, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        yield TLSEcho(listener.getsockname()[1], cert_path,
                      cert.public_bytes(serialization.Encoding.DER))
    finally:
        listener.close()


@pytest.fixture(scope="package")
def ca(tmp_path_factory: pytest.TempPathFactory) -> CertificateAuthority:
    return CertificateAuthority.create(tmp_path_factory.mktemp("proxy-ca"))


@pytest.fixture
def make_proxy(ca: CertificateAuthority) -> Iterator[Callable[..., InterceptProxy]]:
    """Start proxies (deny-all egress unless a policy is given); stop them after the test."""
    started: list[InterceptProxy] = []

    def make(routes: list[Route], policy: EgressPolicy | None = None,
             **kwargs: object) -> InterceptProxy:
        proxy = InterceptProxy(routes, policy or EgressPolicy.allowlist([]), ca, **kwargs)
        proxy.start()
        started.append(proxy)
        return proxy

    yield make
    for proxy in started:
        proxy.stop()
