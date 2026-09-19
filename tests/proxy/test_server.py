"""InterceptProxy end to end, against a local echo upstream (see support.py).

The echo upstream answers with what it actually received, so these tests
assert on the wire: which headers were forwarded, the raw path, body digests.
"""
from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from checkpoint.proxy.server import EgressPolicy, InterceptProxy, Route

from .support import (
    free_port,
    https_on,
    open_tunnel,
    pattern,
    read_head,
    read_response,
    recorded,
    wait_for,
)

HOST = "api.example.test"
TWIN_AUTH = "Bearer twin-bootstrap-token"


@pytest.fixture(scope="module")
def trust(ca) -> ssl.SSLContext:
    """A client context that trusts only Checkpoint's CA (fast to build, unlike bundle.pem)."""
    return ssl.create_default_context(cafile=str(ca.cert_path))


@pytest.fixture
def route(echo_upstream) -> Route:
    return Route(HOST, echo_upstream, auth_header=TWIN_AUTH)


@pytest.fixture
def proxy(make_proxy, route) -> InterceptProxy:
    return make_proxy([route], transparent_port=0)


def _client(proxy: InterceptProxy, trust: ssl.SSLContext, **kwargs) -> httpx.Client:
    return httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", verify=trust, trust_env=False,
                        timeout=10, **kwargs)


def _headers(echo: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in echo["headers"]:
        out.setdefault(name, []).append(value)
    return out


# -- routing, credentials, headers ------------------------------------------------------


def test_routed_host_is_intercepted_and_authorization_replaced(proxy, trust):
    with _client(proxy, trust) as client:
        r = client.get(f"https://{HOST}/echo/hi?x=1", headers={"Authorization": "Bearer real-secret"})
    assert r.status_code == 200
    echo = r.json()
    headers = _headers(echo)
    assert (echo["method"], echo["raw_path"], echo["query"]) == ("GET", "/echo/hi", "x=1")
    assert headers["authorization"] == [TWIN_AUTH]
    assert headers["host"] == [HOST]  # the original Host survives the hop to the twin
    [event] = recorded(proxy, path="/echo/hi?x=1")
    assert (event.via, event.host, event.port, event.method, event.status) == (
        "connect", HOST, 443, "GET", 200)
    assert event.routed and event.allowed and event.error is None


def test_missing_authorization_is_added(proxy, trust):
    with _client(proxy, trust) as client:
        headers = _headers(client.get(f"https://{HOST}/echo").json())
    assert headers["authorization"] == [TWIN_AUTH]


def test_route_without_auth_header_forwards_the_clients_own(make_proxy, echo_upstream, trust):
    proxy = make_proxy([Route(HOST, echo_upstream)])
    with _client(proxy, trust) as client:
        headers = _headers(client.get(f"https://{HOST}/echo", headers={"Authorization": "token mine"}).json())
    assert headers["authorization"] == ["token mine"]


def test_extra_headers_replace_same_named_client_headers(make_proxy, echo_upstream, trust):
    proxy = make_proxy([Route(HOST, echo_upstream, extra_headers={"apikey": "twin-key"})])
    with _client(proxy, trust) as client:
        headers = _headers(client.get(f"https://{HOST}/echo", headers={"apikey": "client-key"}).json())
    assert headers["apikey"] == ["twin-key"]


def test_hop_by_hop_headers_are_not_forwarded(proxy, trust):
    with _client(proxy, trust) as client:
        r = client.get(f"https://{HOST}/echo", headers={
            "Connection": "keep-alive, X-Hop", "X-Hop": "1", "Keep-Alive": "timeout=5",
            "Proxy-Authorization": "Basic Zm9vOmJhcg==", "X-End-To-End": "kept",
        })
    headers = _headers(r.json())
    assert headers["x-end-to-end"] == ["kept"]
    for hop in ("connection", "x-hop", "keep-alive", "proxy-authorization"):
        assert hop not in headers


def test_percent_encoding_in_the_path_is_preserved(proxy, trust):
    with _client(proxy, trust) as client:
        echo = client.get(f"https://{HOST}/repos/o/r/contents/dir%2Ffile.md").json()
    assert echo["raw_path"] == "/repos/o/r/contents/dir%2Ffile.md"


def test_subdomains_route_through_the_parent_domain(make_proxy, echo_upstream, trust):
    proxy = make_proxy([Route("example.test", echo_upstream, auth_header=TWIN_AUTH)])
    with _client(proxy, trust) as client:
        r = client.get("https://project-ref.example.test/echo/sub")
        assert _headers(r.json())["host"] == ["project-ref.example.test"]
        with pytest.raises(httpx.ProxyError, match="403"):
            client.get("https://evilexample.test/")


def test_routes_can_change_at_runtime(make_proxy, echo_upstream, trust):
    proxy = make_proxy([])
    with _client(proxy, trust) as client, pytest.raises(httpx.ProxyError, match="403"):
        client.get(f"https://{HOST}/echo")
    proxy.set_routes([Route(HOST, echo_upstream)])
    with _client(proxy, trust) as client:
        assert client.get(f"https://{HOST}/echo").status_code == 200


# -- HTTP/1.1 semantics -------------------------------------------------------------------


def test_keep_alive_serves_many_requests_on_one_connection(proxy, trust):
    tls = open_tunnel(proxy.port, HOST, trust)
    conn = https_on(tls, HOST)
    for i in range(5):
        conn.request("GET", f"/echo/{i}")
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["raw_path"] == f"/echo/{i}"
    assert conn.sock is tls  # never redialled
    conn.close()


def test_chunked_request_body(proxy, trust):
    parts = [b"alpha" * 1000, b"beta" * 2000, b"gamma"]
    with _client(proxy, trust) as client:
        echo = client.post(f"https://{HOST}/echo", content=iter(parts)).json()
    body = b"".join(parts)
    assert echo["body_len"] == len(body)
    assert echo["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert "transfer-encoding" in _headers(echo) or "content-length" in _headers(echo)


def test_large_bodies_both_ways(proxy, trust):
    size = 5 * 1024 * 1024
    upload = pattern(size)
    with _client(proxy, trust) as client:
        echo = client.put(f"https://{HOST}/echo/upload", content=upload).json()
        download = client.get(f"https://{HOST}/bytes/{size}")
    assert echo["body_len"] == size
    assert echo["body_sha256"] == hashlib.sha256(upload).hexdigest()
    assert download.content == pattern(size)
    [up] = recorded(proxy, path="/echo/upload")
    [down] = recorded(proxy, path=f"/bytes/{size}")
    assert up.request_bytes == size and down.response_bytes == size


def test_responses_are_streamed_not_buffered(proxy, trust):
    with _client(proxy, trust) as client, client.stream("GET", f"https://{HOST}/drip") as r:
        started = time.monotonic()
        lines = r.iter_lines()
        assert next(lines) == "first"
        first_after = time.monotonic() - started
        assert next(lines) == "second"
    assert first_after < 0.8, "the first chunk waited for the whole body"


def test_head_204_and_304_have_no_body_and_keep_the_connection(proxy, trust):
    tls = open_tunnel(proxy.port, HOST, trust)
    conn = https_on(tls, HOST)
    conn.request("HEAD", "/sized")
    head = conn.getresponse()
    assert head.status == 200 and head.getheader("content-length") == "1234"
    assert head.read() == b""
    conn.request("DELETE", "/no-content")
    empty = conn.getresponse()
    assert empty.status == 204 and empty.read() == b""
    conn.request("GET", "/not-modified", headers={"If-None-Match": '"v1"'})
    cached = conn.getresponse()
    assert cached.status == 304 and cached.getheader("etag") == '"v1"' and cached.read() == b""
    conn.request("GET", "/echo/after")  # still in sync: no stray body bytes
    assert json.loads(conn.getresponse().read())["raw_path"] == "/echo/after"
    assert conn.sock is tls
    conn.close()


def test_expect_100_continue(proxy, trust):
    tls = open_tunnel(proxy.port, HOST, trust)
    tls.sendall(f"POST /echo HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 11\r\n"
                "Expect: 100-continue\r\n\r\n".encode())
    assert read_head(tls).startswith(b"HTTP/1.1 100")
    tls.sendall(b"hello world")
    head, body = read_response(tls)
    assert head.startswith(b"HTTP/1.1 200")
    echo = json.loads(body)
    assert echo["body_len"] == 11 and "expect" not in _headers(echo)
    tls.close()


def test_http10_connect_is_accepted(proxy, trust):
    """Python's http.client sent HTTP/1.0 CONNECTs (without Host) before 3.12."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"CONNECT {HOST}:443 HTTP/1.0\r\n\r\n".encode())
    assert b" 200 " in read_head(sock)
    conn = https_on(trust.wrap_socket(sock, server_hostname=HOST), HOST)
    conn.request("GET", "/echo/old")
    assert conn.getresponse().status == 200
    conn.close()


def test_client_hello_pipelined_behind_connect(proxy, trust):
    """CONNECT and the ClientHello in one write: the case asyncio's start_tls() drops."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = trust.wrap_bio(incoming, outgoing, server_hostname=HOST)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode() + outgoing.read())
    assert read_head(sock).startswith(b"HTTP/1.1 200")

    def pump() -> None:
        if outgoing.pending:
            sock.sendall(outgoing.read())
        incoming.write(sock.recv(65536))

    while True:
        try:
            tls.do_handshake()
            break
        except ssl.SSLWantReadError:
            pump()
    tls.write(f"GET /echo/pipelined HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
    sock.sendall(outgoing.read())
    response = b""
    while b"/echo/pipelined" not in response:
        try:
            response += tls.read(65536)
        except ssl.SSLWantReadError:
            pump()
    assert response.startswith(b"HTTP/1.1 200")
    sock.close()


# -- failures surface as HTTP errors, not hangs ---------------------------------------------


def test_unreachable_upstream_is_a_502(make_proxy, trust):
    proxy = make_proxy([Route(HOST, f"http://127.0.0.1:{free_port()}")])
    with _client(proxy, trust) as client:
        r = client.get(f"https://{HOST}/anything")
    assert r.status_code == 502
    [event] = recorded(proxy, path="/anything")
    assert event.status == 502 and event.error


def test_slow_upstream_is_a_504(make_proxy, echo_upstream, trust):
    proxy = make_proxy([Route(HOST, echo_upstream)], upstream_timeout=0.5)
    with _client(proxy, trust) as client:
        assert client.get(f"https://{HOST}/slow?s=3").status_code == 504


def test_origin_form_request_to_the_proxy_itself_is_rejected(proxy):
    r = httpx.get(f"http://127.0.0.1:{proxy.port}/", trust_env=False)
    assert r.status_code == 400
    assert "intercept proxy" in r.text


# -- egress policy -------------------------------------------------------------------------


def test_denied_connect_is_a_403_and_recorded(proxy):
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(b"CONNECT blocked.example:443 HTTP/1.1\r\nHost: blocked.example:443\r\n\r\n")
    head, body = read_response(sock)
    sock.close()
    assert head.startswith(b"HTTP/1.1 403")
    assert b"Checkpoint's sandbox blocked egress to blocked.example:443" in body
    [event] = recorded(proxy, host="blocked.example")
    assert (event.via, event.method, event.status, event.routed, event.allowed) == (
        "connect", "CONNECT", 403, False, False)


def test_allowed_unrouted_host_is_tunnelled_without_decryption(make_proxy, tls_echo):
    proxy = make_proxy([], EgressPolicy.allowlist([f"localhost:{tls_echo.port}"]))
    tls = open_tunnel(proxy.port, "localhost", tls_echo.client_context(), port=tls_echo.port)
    # The client trusts only the echo server's own certificate and got exactly
    # it: the proxy relayed the TLS session instead of terminating it.
    assert tls.getpeercert(binary_form=True) == tls_echo.cert_der
    tls.sendall(b"ping")
    assert tls.recv(4) == b"ping"
    tls.close()
    [event] = recorded(proxy, host="localhost")
    assert (event.via, event.routed, event.allowed, event.status) == ("connect", False, True, 200)
    assert event.request_bytes > 4 and event.response_bytes > 4


def test_absolute_form_plain_http(proxy):
    with httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", trust_env=False, timeout=10) as client:
        routed = client.get(f"http://{HOST}/echo/plain")
        denied = client.get("http://blocked.example/page")
    echo = routed.json()
    assert echo["raw_path"] == "/echo/plain"
    assert _headers(echo)["host"] == [HOST]
    assert _headers(echo)["authorization"] == [TWIN_AUTH]
    assert denied.status_code == 403
    assert "blocked egress to blocked.example:80" in denied.text
    assert recorded(proxy, via="http", host=HOST, status=200)
    assert recorded(proxy, via="http", host="blocked.example", status=403, allowed=False)


# -- transparent mode (the Docker DNS-hijack path) --------------------------------------------


def _direct_tls(proxy: InterceptProxy, sni: str | None, context: ssl.SSLContext) -> ssl.SSLSocket:
    sock = socket.create_connection(("127.0.0.1", proxy.transparent_port), timeout=10)
    return context.wrap_socket(sock, server_hostname=sni)


def test_transparent_mode_routes_by_sni(proxy, trust):
    conn = https_on(_direct_tls(proxy, HOST, trust), HOST)
    conn.request("GET", "/echo/transparent", headers={"Authorization": "token agent"})
    response = conn.getresponse()
    echo = json.loads(response.read())
    conn.close()
    assert response.status == 200
    assert _headers(echo)["host"] == [HOST]
    assert _headers(echo)["authorization"] == [TWIN_AUTH]
    assert recorded(proxy, via="transparent", path="/echo/transparent", routed=True)


def test_transparent_mode_answers_unrouted_denied_hosts_with_a_readable_403(proxy, trust):
    conn = https_on(_direct_tls(proxy, "blocked.example", trust), "blocked.example")
    conn.request("GET", "/")
    response = conn.getresponse()
    assert response.status == 403
    assert b"blocked egress to blocked.example:" in response.read()
    conn.close()


def test_transparent_mode_tunnels_allowed_unrouted_hosts(make_proxy, tls_echo):
    proxy = make_proxy([], EgressPolicy.allowlist(["localhost"]), transparent_port=0)
    dialed: list[tuple[str, int]] = []
    real_dial = proxy._dial

    async def dial_echo_server(host: str, port: int):
        # Stand-in for DNS: the real destination would be localhost:<this port>.
        dialed.append((host, port))
        return await real_dial("127.0.0.1", tls_echo.port)

    proxy._dial = dial_echo_server
    tls = _direct_tls(proxy, "localhost", tls_echo.client_context())
    assert tls.getpeercert(binary_form=True) == tls_echo.cert_der
    tls.sendall(b"pong")
    assert tls.recv(4) == b"pong"
    tls.close()
    assert dialed == [("localhost", proxy.transparent_port)]
    recorded(proxy, via="transparent", host="localhost", allowed=True)


def test_transparent_mode_without_sni_is_refused(proxy):
    anything = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    anything.check_hostname = False
    anything.verify_mode = ssl.CERT_NONE
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        _direct_tls(proxy, None, anything)
    wait_for(lambda: [e for e in proxy.events() if e.error and "SNI" in e.error])


# -- robustness ---------------------------------------------------------------------------------


def test_misbehaving_clients_do_not_affect_others(proxy, trust):
    garbage = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    garbage.sendall(b"\x00\x01\x02 not http at all\r\n\r\n")
    assert read_head(garbage).startswith(b"HTTP/1.1 400")
    garbage.close()

    half_handshake = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    half_handshake.sendall(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    read_head(half_handshake)
    half_handshake.sendall(b"\x16\x03\x01\x00\x05hello")  # a truncated, bogus ClientHello
    half_handshake.close()

    distrustful = ssl.create_default_context()  # does NOT trust Checkpoint's CA
    with pytest.raises(ssl.SSLCertVerificationError):
        open_tunnel(proxy.port, HOST, distrustful)
    wait_for(lambda: [e for e in proxy.events() if e.error and "TLS handshake failed" in e.error])

    with _client(proxy, trust) as client:
        assert client.get(f"https://{HOST}/echo/still-fine").status_code == 200


def test_concurrent_clients(proxy, trust):
    def one(i: int) -> tuple[int, str, str]:
        body = f"request-{i}-".encode() * (i + 1)
        with _client(proxy, trust) as client:
            r = client.post(f"https://{HOST}/echo/concurrent/{i}", content=body)
        return r.status_code, r.json()["body_sha256"], hashlib.sha256(body).hexdigest()

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(one, range(20)))
    assert all(status == 200 and got == want for status, got, want in results)
    wait_for(lambda: len([e for e in proxy.events() if (e.path or "").startswith("/echo/concurrent/")]) == 20)


def test_idle_keep_alive_connections_are_closed(make_proxy, route):
    proxy = make_proxy([route], idle_timeout=0.5)
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    # One complete keep-alive exchange, sent in a single write so a busy
    # machine cannot make the request itself look idle...
    sock.sendall(f"GET http://{HOST}/echo HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    head, _ = read_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    idle_since = time.monotonic()
    # ...then silence: the proxy must hang up rather than hold the socket forever.
    assert sock.recv(1) == b""
    assert time.monotonic() - idle_since >= 0.4
    sock.close()


def test_stop_closes_open_connections_promptly(ca, route, trust):
    proxy = InterceptProxy([route], EgressPolicy.open(), ca)
    proxy.start()
    tls = open_tunnel(proxy.port, HOST, trust)
    started = time.perf_counter()
    proxy.stop()
    assert time.perf_counter() - started < 1.0
    tls.settimeout(2)
    try:
        assert tls.recv(1) == b""
    except (ConnectionError, ssl.SSLError):
        pass
    tls.close()


def test_start_and_stop_are_fast_and_leave_nothing_behind(ca, route):
    threads_before = set(threading.enumerate())
    proxy = InterceptProxy([route], EgressPolicy.open(), ca, transparent_port=0)
    started = time.perf_counter()
    with proxy:
        start_time = time.perf_counter() - started
        ports = (proxy.port, proxy.transparent_port)
        stopping = time.perf_counter()
    stop_time = time.perf_counter() - stopping
    assert start_time < 0.5 and stop_time < 0.5, (start_time, stop_time)
    assert set(threading.enumerate()) - threads_before == set()
    for port in ports:  # both listeners really closed
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))


def test_start_failure_is_raised_and_cleaned_up(ca, route):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        threads_before = set(threading.enumerate())
        proxy = InterceptProxy([route], EgressPolicy.open(), ca, port=taken.getsockname()[1])
        with pytest.raises(OSError):
            proxy.start()
        assert set(threading.enumerate()) - threads_before == set()
        proxy.stop()  # harmless after a failed start


# -- introspection ------------------------------------------------------------------------------


def test_client_env(proxy, ca):
    env = proxy.client_env()
    url = f"http://127.0.0.1:{proxy.port}"
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert env[key] == url
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPLIB2_CA_CERTS"):
        assert env[key] == str(ca.bundle_path)
    assert env["NODE_EXTRA_CA_CERTS"] == str(ca.cert_path)
    assert env["NODE_USE_ENV_PROXY"] == "1"


def test_client_env_requires_a_running_proxy(ca):
    proxy = InterceptProxy([], EgressPolicy.open(), ca)
    with pytest.raises(RuntimeError):
        proxy.client_env()
    with proxy:
        assert proxy.client_env()
    with pytest.raises(RuntimeError):
        proxy.client_env()


def test_events_can_be_cleared(proxy, trust):
    with _client(proxy, trust) as client:
        client.get(f"https://{HOST}/echo")
    wait_for(proxy.events)
    proxy.clear_events()
    assert proxy.events() == []
