"""Checkpoint's intercepting HTTP(S) proxy.

An agent under test either runs with ``HTTPS_PROXY`` pointed here (local runs,
see :meth:`InterceptProxy.client_env`) or has its SaaS hostnames resolved to
this process (the Docker sidecar's transparent listener). For a host with a
:class:`Route`, the proxy terminates TLS with a certificate from Checkpoint's
CA, parses HTTP/1.1 with h11 and replays each request against the route's twin,
stamping in the twin's bootstrap credential — so real SDKs reach the fakes
unmodified. Every other host is decided by an :class:`EgressPolicy`: allowed
traffic gets a blind TCP tunnel that is never decrypted, denied traffic a 403.

TLS is driven through ``ssl.MemoryBIO`` rather than asyncio's ``start_tls()``.
``start_tls()`` does work on the Windows Proactor loop, but it only sees bytes
that arrive *after* the upgrade, and this proxy has usually read the
ClientHello already: h11 buffers one pipelined behind a CONNECT, and the
transparent listener must parse it for SNI before it can decide whether to
intercept or tunnel at all. With ``start_tls()`` those handshakes hang.
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import ipaddress
import json
import logging
import ssl
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import h11
import httpx

from .ca import CertificateAuthority, normalize_host

log = logging.getLogger("checkpoint.proxy")

# Official API hosts of the major model providers, for EgressPolicy.allowlist():
# an agent under test usually still needs its own LLM while everything else is
# sandboxed. Globs follow EgressPolicy's pattern rules.
LLM_PROVIDER_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "api.anthropic.com",
    # Google: the Gemini API, plus Vertex AI's global and regional endpoints and
    # the OAuth endpoint Vertex clients mint their access tokens from.
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "*-aiplatform.googleapis.com",
    "oauth2.googleapis.com",
    # Azure OpenAI / Azure AI Foundry resources.
    "*.openai.azure.com",
    "*.services.ai.azure.com",
    "*.cognitiveservices.azure.com",
    # AWS Bedrock runtime, every region, including the FIPS endpoints.
    "bedrock-runtime.*.amazonaws.com",
    "bedrock-runtime-fips.*.amazonaws.com",
    "api.mistral.ai",
    "api.groq.com",
    "api.together.xyz",
    "api.together.ai",
    "api.fireworks.ai",
    "openrouter.ai",
    "api.deepseek.com",
    "api.x.ai",
    "api.cohere.com",
    "api.cohere.ai",
    "api.perplexity.ai",
    # Loopback, so local model servers (Ollama, vLLM, LM Studio, llama.cpp) work.
    "localhost",
    "127.0.0.1",
    "::1",
)

# Connection-specific fields (RFC 9110 §7.6.1) plus the legacy Proxy-Connection:
# they describe a single hop and must not be forwarded. Headers named in a
# Connection field are dropped as well.
_HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-connection", b"proxy-authenticate",
    b"proxy-authorization", b"te", b"trailer", b"transfer-encoding", b"upgrade",
})
_NO_PROXY = "localhost,127.0.0.1,::1"
_CHUNK = 64 * 1024
_CONNECT_TIMEOUT = 10.0
_MAX_HEADER_BYTES = 64 * 1024
_MAX_CLIENT_HELLO = 64 * 1024
_MAX_EVENTS = 10_000
# "This one connection went away" (ssl.SSLError and TimeoutError are OSErrors),
# as opposed to a bug in the proxy, which is logged loudly.
_CONNECTION_ERRORS = (OSError, EOFError, h11.ProtocolError)


@dataclass(frozen=True)
class Route:
    """Send requests for ``domain`` and every subdomain of it to ``upstream_url``.

    ``supabase.co`` therefore also routes ``<project>.supabase.co`` (but never
    ``evilsupabase.co``). When ``auth_header`` is set it replaces whatever
    ``Authorization`` the client sent, which is what lets an agent holding a
    real-looking or missing token authenticate against the twin.
    ``extra_headers`` are set (replacing same-named ones) on every request.
    The client's ``Host`` header is always forwarded untouched.
    """

    domain: str
    upstream_url: str
    auth_header: str | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", normalize_host(self.domain))
        if not self.domain:
            raise ValueError("Route domain must not be empty")
        parts = urlsplit(self.upstream_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Route upstream_url must be an http(s) URL, got {self.upstream_url!r}")


@dataclass(frozen=True)
class EgressPolicy:
    """Where NON-routed traffic may go. Routed hosts never consult it: their
    traffic is answered by a local twin and never leaves the machine.

    Patterns: ``host`` (exact), ``*.suffix`` (any subdomain, not the apex
    itself), or any other shell-style glob such as
    ``*-aiplatform.googleapis.com``; each may end in ``:port`` to restrict the
    port (IPv6 literals with a port are written ``[::1]:8080``).
    """

    patterns: tuple[str, ...] | None = None  # None: allow everything
    _rules: tuple[tuple[str, int | None], ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.patterns is not None:
            object.__setattr__(self, "_rules", tuple(
                _split_authority(p.strip(), default_port=None) for p in self.patterns
            ))

    @classmethod
    def open(cls) -> EgressPolicy:
        """Allow every destination."""
        return cls()

    @classmethod
    def allowlist(cls, patterns: Iterable[str]) -> EgressPolicy:
        """Allow only destinations matching one of ``patterns``; deny the rest."""
        return cls(tuple(patterns))

    def allows(self, host: str, port: int) -> bool:
        if self.patterns is None:
            return True
        host = normalize_host(host)
        return any(
            fnmatch.fnmatchcase(host, glob) and want in (None, port)
            for glob, want in self._rules
        )


@dataclass(frozen=True)
class ProxyEvent:
    """One proxied request, tunnel, refusal or failed connection.

    ``via`` says how the client reached the proxy: ``"connect"`` (CONNECT
    tunnel), ``"http"`` (absolute-form plain-HTTP request) or
    ``"transparent"`` (direct TLS to the transparent listener). For tunnels,
    ``request_bytes``/``response_bytes`` count raw bytes each way; for
    requests, body bytes.
    """

    timestamp: float
    via: str
    host: str
    port: int
    routed: bool
    allowed: bool
    method: str | None = None
    path: str | None = None
    status: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    error: str | None = None


def routes_to_json(routes: Iterable[Route]) -> str:
    """Serialize routes for ``python -m checkpoint.proxy --routes`` (runner → sidecar)."""
    return json.dumps({
        r.domain: {
            "upstream_url": r.upstream_url,
            "auth_header": r.auth_header,
            "extra_headers": dict(r.extra_headers),
        }
        for r in routes
    })


def routes_from_json(text: str) -> list[Route]:
    """Inverse of :func:`routes_to_json`; a bare URL string is shorthand for ``{"upstream_url": url}``."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("routes JSON must be an object mapping domain -> upstream")
    routes = []
    for domain, spec in data.items():
        if isinstance(spec, str):
            routes.append(Route(domain, spec))
        elif isinstance(spec, dict):
            try:
                routes.append(Route(domain, **spec))
            except TypeError as exc:
                raise ValueError(f"bad route for {domain!r}: {exc}") from exc
        else:
            raise ValueError(f"bad route for {domain!r}: expected a URL string or an object")
    return routes


class InterceptProxy:
    """The proxy server. It runs its own asyncio loop on a daemon thread.

    ``start()`` binds and returns the forward-proxy port; ``stop()`` closes the
    listeners and every open connection; it is also a context manager. With
    ``transparent_port`` set it additionally accepts direct TLS connections
    (clients whose DNS resolved a SaaS host to this proxy) and picks the
    destination from the ClientHello's SNI.
    """

    def __init__(
        self,
        routes: Iterable[Route],
        policy: EgressPolicy,
        ca: CertificateAuthority,
        host: str = "127.0.0.1",
        port: int = 0,
        transparent_port: int | None = None,
        *,
        idle_timeout: float = 60.0,
        upstream_timeout: float = 120.0,
    ) -> None:
        self.policy = policy
        self.ca = ca
        self.host = host
        self.idle_timeout = idle_timeout
        self.upstream_timeout = upstream_timeout
        self.port: int | None = None
        self.transparent_port: int | None = None
        self._bind_port = port
        self._bind_transparent_port = transparent_port
        self._routes = _index(routes)
        self._events: deque[ProxyEvent] = deque(maxlen=_MAX_EVENTS)
        self._events_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._closing = False

    # -- public API -----------------------------------------------------------

    def start(self) -> int:
        """Bind the listener(s), start serving on a background thread and return the proxy port."""
        if self._thread is not None:
            raise RuntimeError("InterceptProxy is already running")
        loop = asyncio.new_event_loop()
        started = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            try:
                loop.run_until_complete(self._open())
            except BaseException as exc:
                failure.append(exc)
            started.set()
            if not failure:
                loop.run_forever()
            # Release what the loop owns (resolver threads, async generators)
            # so a stopped proxy leaves no threads or sockets behind.
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

        self._closing = False
        self._loop = loop
        self._thread = threading.Thread(target=run, name="checkpoint-proxy", daemon=True)
        self._thread.start()
        started.wait()
        if failure:
            self._thread.join()
            self._loop = self._thread = None
            raise failure[0]
        assert self.port is not None
        return self.port

    def stop(self) -> None:
        """Close the listeners and all connections, then end the loop thread. Idempotent."""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), loop).result(timeout=10)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            self._loop = self._thread = None
            self.port = self.transparent_port = None  # client_env() must not hand out a dead URL

    def __enter__(self) -> InterceptProxy:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def set_routes(self, routes: Iterable[Route]) -> None:
        """Replace the route table; connections already intercepted keep their route."""
        self._routes = _index(routes)

    def route_for(self, host: str) -> Route | None:
        """The most specific route for ``host``: an exact domain first, then each parent domain."""
        routes = self._routes
        host = normalize_host(host)
        if host in routes:
            return routes[host]
        if _is_ip(host):
            return None
        labels = host.split(".")
        for i in range(1, len(labels)):
            route = routes.get(".".join(labels[i:]))
            if route is not None:
                return route
        return None

    def events(self) -> list[ProxyEvent]:
        """Everything the proxy has handled so far (the most recent 10,000 events), oldest first."""
        with self._events_lock:
            return list(self._events)

    def clear_events(self) -> None:
        with self._events_lock:
            self._events.clear()

    def client_env(self) -> dict[str, str]:
        """The environment an agent subprocess needs to use this proxy and trust its CA."""
        if self.port is None:
            raise RuntimeError("start() the proxy before asking for its client environment")
        host = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(self.host, self.host)
        url = f"http://{_authority(host, self.port)}"
        bundle, ca_cert = str(self.ca.bundle_path), str(self.ca.cert_path)
        return {
            # Proxy discovery, in both cases: curl reads only lowercase
            # http_proxy, while Python, Go and Node read either spelling.
            "HTTPS_PROXY": url,
            "https_proxy": url,
            "HTTP_PROXY": url,
            "http_proxy": url,
            # Local twins, local model servers and this proxy are never proxied.
            "NO_PROXY": _NO_PROXY,
            "no_proxy": _NO_PROXY,
            # Trust stores. Each gets bundle.pem (public roots + our CA) so that
            # non-intercepted HTTPS keeps verifying:
            # OpenSSL-based stacks — Python's ssl/urllib/httpx, Ruby, Go on Linux.
            "SSL_CERT_FILE": bundle,
            # requests (and botocore, which honours it too) ship certifi otherwise.
            "REQUESTS_CA_BUNDLE": bundle,
            # curl and libcurl bindings.
            "CURL_CA_BUNDLE": bundle,
            # httplib2 — google-api-python-client, i.e. the Gmail/Drive SDKs —
            # bundles its own roots and ignores all of the above.
            "HTTPLIB2_CA_CERTS": bundle,
            # Node appends these to its built-in roots, so the CA alone is right.
            "NODE_EXTRA_CA_CERTS": ca_cert,
            # Node >= 24.5 / 22.21 honours HTTP(S)_PROXY only when asked to.
            "NODE_USE_ENV_PROXY": "1",
        }

    # -- lifecycle (loop thread) -------------------------------------------------

    async def _open(self) -> None:
        try:
            server = await asyncio.start_server(
                functools.partial(self._accept, transparent=False), self.host, self._bind_port
            )
            self._servers.append(server)
            self.port = server.sockets[0].getsockname()[1]
            if self._bind_transparent_port is not None:
                server = await asyncio.start_server(
                    functools.partial(self._accept, transparent=True),
                    self.host, self._bind_transparent_port,
                )
                self._servers.append(server)
                self.transparent_port = server.sockets[0].getsockname()[1]
        except BaseException:
            await self._close()
            raise

    async def _close(self) -> None:
        self._closing = True
        for server in self._servers:
            server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for server in self._servers:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    await server.wait_closed()
        self._servers.clear()
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    def _client_for(self, url: httpx.URL) -> httpx.AsyncClient:
        """The upstream client for ``url``'s scheme, created on first use.

        Twins are plain ``http://``, so the common client never needs a CA
        store — and loading certifi's costs ~0.7 s on Windows (OpenSSL 3),
        which would dominate start(). Its TLS context trusts nothing, so an
        https URL could only ever fail closed on it. ``https://`` upstreams (an
        allowlisted absolute-form request) get a verifying client.
        """
        client = self._clients.get(url.scheme)
        if client is None:
            verify: ssl.SSLContext | bool = (
                ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if url.scheme == "http" else True
            )
            client = self._clients[url.scheme] = httpx.AsyncClient(
                verify=verify,
                # Never let our own environment's proxy settings (possibly
                # pointing at this very proxy) redirect traffic meant for a twin.
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(self.upstream_timeout, connect=_CONNECT_TIMEOUT),
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
            )
        return client

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                      transparent: bool) -> None:
        """Per-connection entry point: whatever happens, only this connection is affected."""
        task = asyncio.current_task()
        if self._closing or task is None:
            writer.close()
            return
        self._tasks.add(task)
        try:
            if transparent:
                await self._serve_transparent(reader, writer)
            else:
                await self._serve_front(reader, writer)
        except _CONNECTION_ERRORS as exc:
            log.debug("connection ended: %r", exc)
        except Exception:
            log.warning("unexpected error on a proxied connection", exc_info=True)
        finally:
            self._tasks.discard(task)
            writer.close()

    # -- entry modes ------------------------------------------------------------

    async def _serve_front(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A client using us as its proxy: CONNECT, or absolute-form plain-HTTP requests."""
        stream = _PlainStream(reader, writer)
        h = _server_connection()
        while (request := await self._next_request(h, stream)) is not None:
            if request.method == b"CONNECT":
                if await self._connect(h, stream, request, reader, writer):
                    return
            else:
                await self._absolute(h, stream, request)
            if not _next_cycle(h):
                return

    async def _serve_transparent(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> None:
        """A client that resolved a SaaS host to us and started TLS directly (Docker DNS hijack)."""
        started = time.time()
        local_host, port = writer.get_extra_info("sockname")[:2]
        try:
            async with asyncio.timeout(self.idle_timeout):
                hello, sni = await _read_client_hello(reader)
        except (_NotTLSError, EOFError, TimeoutError) as exc:
            self._record(ProxyEvent(started, "transparent", local_host, port, routed=False,
                                    allowed=False, error=f"not a TLS ClientHello: {exc!r}"))
            return
        if not hello:
            return  # connected and left without a byte (a port probe)
        if not sni:
            self._record(ProxyEvent(
                started, "transparent", local_host, port, routed=False, allowed=False,
                error="TLS ClientHello without SNI: no way to tell which host the client wanted",
            ))
            return
        host = normalize_host(sni)
        route = self.route_for(host)
        if route is None and self.policy.allows(host, port):
            upstream = await self._dial_or_record(host, port, "transparent", started)
            if upstream is not None:
                await self._tunnel(reader, writer, upstream, host, port, hello, "transparent", started)
            return
        # Routed hosts are intercepted. Denied ones are terminated too, so the
        # client reads the same explanatory 403 a CONNECT client would get
        # instead of an opaque TLS failure.
        await self._intercept(reader, writer, route, host, port, hello, "transparent")

    async def _connect(self, h: h11.Connection, stream: _PlainStream, request: h11.Request,
                       reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Answer a CONNECT. True once the connection belongs to a tunnel or an interception."""
        started = time.time()
        try:
            host, port = _split_authority(request.target.decode("ascii"), default_port=443)
        except ValueError:
            await _reject(h, stream, 400, "CONNECT needs a host:port target.")
            return True
        assert port is not None
        route = self.route_for(host)
        if route is None and not self.policy.allows(host, port):
            await self._deny(h, stream, request, host, port, "connect", started)
            return False
        upstream = None
        if route is None:
            upstream = await self._dial_or_record(host, port, "connect", started)
            if upstream is None:
                await _respond_text(h, stream, 502, f"Checkpoint proxy could not connect to "
                                    f"{_authority(host, port)}.", request.method)
                return False
        await stream.send(h.send(h11.Response(status_code=200, headers=[],
                                              reason=b"Connection Established")))
        initial, _ = h.trailing_data
        if upstream is None:
            await self._intercept(reader, writer, route, host, port, initial, "connect")
        else:
            await self._tunnel(reader, writer, upstream, host, port, initial, "connect", started)
        return True

    async def _absolute(self, h: h11.Connection, stream: _PlainStream, request: h11.Request) -> None:
        """An absolute-form request (``GET http://host/path``) sent to the proxy itself."""
        started = time.time()
        target = request.target.decode("ascii", "replace")
        try:
            parts = urlsplit(target)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            parts, port = None, 0
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
            await _reject(h, stream, 400, "This is Checkpoint's intercept proxy: send CONNECT "
                          "host:port or an absolute-form http:// request.", request.method)
            return
        host = normalize_host(parts.hostname)
        path = _origin_form(target)
        route = self.route_for(host)
        if route is not None:
            await self._forward(h, stream, request, route, route.upstream_url, path,
                                host, port, "http", started)
        elif self.policy.allows(host, port):
            origin = f"{parts.scheme}://{_authority(host, port)}"
            await self._forward(h, stream, request, None, origin, path, host, port, "http", started)
        else:
            await self._deny(h, stream, request, host, port, "http", started)

    # -- interception -------------------------------------------------------------

    async def _intercept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         route: Route | None, host: str, port: int, initial: bytes,
                         via: str) -> None:
        """Serve HTTP/1.1 to a client that believes it reached ``host``.

        TLS is terminated with a leaf certificate for ``host`` if the client
        starts a handshake (a plain-HTTP CONNECT tunnel is served as-is).
        ``route=None`` answers every request with the egress-denied 403.
        """
        if not initial:
            async with asyncio.timeout(self.idle_timeout):
                initial = await reader.read(_CHUNK)
            if not initial:
                return
        stream: _PlainStream | _TLSStream
        if initial[0] == 0x16:  # TLS handshake record
            stream = _TLSStream(reader, writer, self.ca.server_context(host), initial)
            try:
                async with asyncio.timeout(self.idle_timeout):
                    await stream.handshake()
            except (OSError, EOFError) as exc:
                self._record(ProxyEvent(
                    time.time(), via, host, port, routed=route is not None,
                    allowed=route is not None,
                    error=f"TLS handshake failed ({exc!r}); does the client trust "
                          f"Checkpoint's CA at {self.ca.cert_path}?",
                ))
                return
        else:
            stream = _PlainStream(reader, writer, initial)
        try:
            h = _server_connection()
            while (request := await self._next_request(h, stream)) is not None:
                started = time.time()
                if request.method == b"CONNECT":
                    await _reject(h, stream, 405, "CONNECT inside a tunnel is not supported.")
                    return
                if route is None:
                    await self._deny(h, stream, request, host, port, via, started)
                else:
                    path = _origin_form(request.target.decode("ascii", "replace"))
                    await self._forward(h, stream, request, route, route.upstream_url, path,
                                        host, port, via, started)
                if not _next_cycle(h):
                    return
        finally:
            await stream.aclose()

    async def _forward(self, h: h11.Connection, stream: _PlainStream | _TLSStream,
                       request: h11.Request, route: Route | None, base_url: str, path: str,
                       host: str, port: int, via: str, started: float) -> None:
        """Replay one request against ``base_url`` and stream the response back."""
        exchange = _Exchange()
        method = request.method.decode("ascii")
        try:
            headers = _forward_headers(request, route, _authority_header(host, port, via))
            body = self._request_body(h, stream, exchange) if _has_body(request) else None
            if body is None:
                await self._next_event(h, stream)  # the request's EndOfMessage
            url = _upstream_url(base_url, path)
            upstream = httpx.Request(method, url, headers=headers, content=body)
            try:
                response = await self._client_for(url).send(upstream, stream=True)
            except (httpx.HTTPError, OSError) as exc:
                if exchange.client_failed:
                    raise
                exchange.status = 504 if isinstance(exc, httpx.TimeoutException) else 502
                exchange.error = f"upstream {base_url} failed: {exc!r}"
                await _respond_text(h, stream, exchange.status,
                                    f"Checkpoint proxy could not get a response for {host} from "
                                    f"{base_url}: {exc!r}", request.method, close=True)
                return
            exchange.status = response.status_code
            try:
                await stream.send(h.send(h11.Response(
                    status_code=response.status_code,
                    headers=_strip_hop_by_hop(response.headers.raw),
                    reason=response.extensions.get("reason_phrase", b""),
                )))
                async for chunk in response.aiter_raw():
                    exchange.response_bytes += len(chunk)
                    await stream.send(h.send(h11.Data(data=chunk)))
                await stream.send(h.send(h11.EndOfMessage()))
            except httpx.HTTPError as exc:
                # The status line is already out: closing the connection is the
                # only way left to tell the client the body is incomplete.
                raise ConnectionAbortedError(f"upstream failed mid-response: {exc!r}") from exc
            finally:
                await response.aclose()
        except BaseException as exc:
            exchange.error = exchange.error or _describe(exc)
            raise
        finally:
            self._record(ProxyEvent(
                started, via, host, port, routed=route is not None, allowed=True,
                method=method, path=path, status=exchange.status,
                request_bytes=exchange.request_bytes, response_bytes=exchange.response_bytes,
                error=exchange.error,
            ))

    async def _request_body(self, h: h11.Connection, stream: _PlainStream | _TLSStream,
                            exchange: _Exchange) -> AsyncIterator[bytes]:
        """Stream the client's request body (Content-Length or chunked) to httpx as it arrives."""
        try:
            if h.they_are_waiting_for_100_continue:
                await stream.send(h.send(h11.InformationalResponse(status_code=100, headers=[])))
            while True:
                event = await self._next_event(h, stream)
                if isinstance(event, h11.Data):
                    exchange.request_bytes += len(event.data)
                    yield bytes(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    return
                else:
                    raise ConnectionResetError("client closed the connection mid-request")
        except BaseException:
            exchange.client_failed = True
            raise

    async def _deny(self, h: h11.Connection, stream: _PlainStream | _TLSStream,
                    request: h11.Request, host: str, port: int, via: str, started: float) -> None:
        await _respond_text(
            h, stream, 403,
            f"Checkpoint's sandbox blocked egress to {_authority(host, port)}: the host is not "
            "routed to a twin and is not on the egress allowlist.",
            request.method,
        )
        is_connect = request.method == b"CONNECT"
        self._record(ProxyEvent(
            started, via, host, port, routed=False, allowed=False,
            method=request.method.decode("ascii"),
            path=None if is_connect else _origin_form(request.target.decode("ascii", "replace")),
            status=403,
        ))

    # -- tunnels ------------------------------------------------------------------

    async def _dial_or_record(self, host: str, port: int, via: str, started: float,
                              ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            return await self._dial(host, port)
        except OSError as exc:
            self._record(ProxyEvent(started, via, host, port, routed=False, allowed=True,
                                    method="CONNECT" if via == "connect" else None,
                                    status=502, error=f"could not connect: {exc!r}"))
            return None

    async def _dial(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Happy Eyeballs (RFC 8305): race the address families instead of
        # waiting out one that refuses — e.g. `localhost` resolving to ::1
        # first, where Windows takes ~2 s to report a refused loopback connect.
        async with asyncio.timeout(_CONNECT_TIMEOUT):
            return await asyncio.open_connection(host, port, happy_eyeballs_delay=0.25)

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter], host: str,
                      port: int, initial: bytes, via: str, started: float) -> None:
        """Relay raw bytes both ways without looking at them: TLS stays end-to-end."""
        up_reader, up_writer = upstream
        counts = [len(initial), 0]
        error: str | None = None

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, index: int) -> None:
            while data := await src.read(_CHUNK):
                counts[index] += len(data)
                dst.write(data)
                await dst.drain()
            if dst.can_write_eof():
                dst.write_eof()  # propagate the half-close

        try:
            if initial:
                up_writer.write(initial)
                await up_writer.drain()
            pumps = (asyncio.create_task(pump(reader, up_writer, 0)),
                     asyncio.create_task(pump(up_reader, writer, 1)))
            try:
                done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                if pending and not any(t.exception() for t in done):
                    # One side finished cleanly; give the other side the idle
                    # window to deliver what is still in flight.
                    await asyncio.wait(pending, timeout=self.idle_timeout)
            finally:
                for task in pumps:
                    task.cancel()
                results = await asyncio.gather(*pumps, return_exceptions=True)
            failures = [r for r in results if isinstance(r, Exception)]
            if failures:
                error = _describe(failures[0])
        except BaseException as exc:
            error = _describe(exc)
            raise
        finally:
            up_writer.close()
            self._record(ProxyEvent(
                started, via, host, port, routed=False, allowed=True,
                method="CONNECT" if via == "connect" else None,
                status=200 if via == "connect" else None,
                request_bytes=counts[0], response_bytes=counts[1], error=error,
            ))

    # -- h11 plumbing ----------------------------------------------------------------

    async def _next_event(self, h: h11.Connection, stream: _PlainStream | _TLSStream) -> h11.Event:
        """The next h11 event, reading from the client as needed (each read bounded by idle_timeout)."""
        while True:
            event = h.next_event()
            if event is h11.NEED_DATA:
                async with asyncio.timeout(self.idle_timeout):
                    h.receive_data(await stream.recv())
            elif event is h11.PAUSED:
                return h11.ConnectionClosed()
            else:
                return event

    async def _next_request(self, h: h11.Connection,
                            stream: _PlainStream | _TLSStream) -> h11.Request | None:
        """Wait for the next request on a connection; None when the client is done with it."""
        try:
            event = await self._next_event(h, stream)
        except TimeoutError:
            return None  # idle keep-alive connection
        except h11.RemoteProtocolError as exc:
            await _reject(h, stream, exc.error_status_hint, f"Malformed HTTP request: {exc}")
            return None
        return event if isinstance(event, h11.Request) else None

    def _record(self, event: ProxyEvent) -> None:
        with self._events_lock:
            self._events.append(event)
        log.debug("%s", event)


# -- streams ---------------------------------------------------------------------------


class _PlainStream:
    """The client connection as-is, with bytes already read replayed first."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 initial: bytes = b"") -> None:
        self._reader = reader
        self._writer = writer
        self._initial = initial

    async def recv(self) -> bytes:
        if self._initial:
            data, self._initial = self._initial, b""
            return data
        return await self._reader.read(_CHUNK)

    async def send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def aclose(self) -> None:
        self._writer.close()


class _TLSStream:
    """Server-side TLS over an accepted connection, driven through ``ssl.MemoryBIO``.

    Bytes the proxy has already consumed (the ClientHello) are fed in first;
    see the module docstring for why asyncio's start_tls() cannot do that.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 context: ssl.SSLContext, initial: bytes) -> None:
        self._reader = reader
        self._writer = writer
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._incoming.write(initial)
        self._eof = False
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_side=True)

    async def handshake(self) -> None:
        while True:
            try:
                self._tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush()
                if not await self._fill():
                    raise ConnectionResetError(
                        "client closed the connection during the TLS handshake"
                    ) from None
        await self._flush()

    async def recv(self) -> bytes:
        while True:
            try:
                data = self._tls.read(_CHUNK)
            except ssl.SSLWantReadError:
                if self._eof:
                    return b""
                await self._flush()
                await self._fill()
                continue
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                return b""  # close_notify, or a client that just hung up
            await self._flush()  # e.g. a TLS 1.3 KeyUpdate reply
            return data

    async def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[self._tls.write(view):]
            await self._flush()

    async def aclose(self) -> None:
        with contextlib.suppress(OSError):  # the peer may be long gone
            with contextlib.suppress(ssl.SSLWantReadError):
                self._tls.unwrap()  # queues our close_notify; theirs is not awaited
            async with asyncio.timeout(1):
                await self._flush()
        self._writer.close()

    async def _fill(self) -> bool:
        data = await self._reader.read(_CHUNK)
        if data:
            self._incoming.write(data)
            return True
        self._incoming.write_eof()
        self._eof = True
        return False

    async def _flush(self) -> None:
        if self._outgoing.pending:
            self._writer.write(self._outgoing.read())
            await self._writer.drain()


# -- helpers ----------------------------------------------------------------------------


@dataclass
class _Exchange:
    """Mutable tally for one forwarded request, turned into a ProxyEvent at the end."""

    status: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    error: str | None = None
    client_failed: bool = False


class _NotTLSError(ValueError):
    pass


def _index(routes: Iterable[Route]) -> dict[str, Route]:
    return {route.domain: route for route in routes}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _authority_header(host: str, port: int, via: str) -> bytes:
    """Host header to use when the client sent none (HTTP/1.0): what it would have sent."""
    default = 80 if via == "http" else 443
    bracketed = f"[{host}]" if ":" in host else host
    return (bracketed if port == default else _authority(host, port)).encode("ascii")


def _split_authority(authority: str, default_port: int | None) -> tuple[str, int | None]:
    """``(host, port)`` from ``host:port``, ``[v6]:port``, ``host`` or a bare IPv6 literal."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
    elif authority.count(":") == 1:
        host, _, port_text = authority.partition(":")
        port = int(port_text)
    else:
        host, port = authority, default_port
    if not host or (port is not None and not 0 < port < 65536):
        raise ValueError(f"invalid host[:port] {authority!r}")
    return normalize_host(host), port


def _origin_form(target: str) -> str:
    """The ``/path?query`` part of a request target, whether origin- or absolute-form."""
    if target.startswith("/"):
        return target
    rest = target.split("://", 1)[1] if "://" in target else target
    cut = min((i for i in (rest.find("/"), rest.find("?")) if i != -1), default=len(rest))
    path = rest[cut:]
    return path if path.startswith("/") else "/" + path


def _upstream_url(base_url: str, path: str) -> httpx.URL:
    """``base_url`` (which may carry a path prefix) joined with the client's raw path.

    ``raw_path`` keeps the client's percent-encoding (``%2F`` in a GitHub
    contents path must not become ``/``).
    """
    base = httpx.URL(base_url)
    prefix = base.raw_path.split(b"?", 1)[0].rstrip(b"/")
    return base.copy_with(raw_path=prefix + path.encode("ascii"))


def _connection_tokens(headers: Iterable[tuple[bytes, bytes]]) -> set[bytes]:
    return {
        token.strip().lower()
        for name, value in headers if name.lower() == b"connection"
        for token in value.split(b",")
    }


def _strip_hop_by_hop(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    headers = list(headers)
    drop = _HOP_BY_HOP | _connection_tokens(headers)
    return [(name, value) for name, value in headers if name.lower() not in drop]


def _forward_headers(request: h11.Request, route: Route | None,
                     host_header: bytes) -> list[tuple[bytes, bytes]]:
    """The client's headers minus hop-by-hop ones, with the route's credentials stamped in.

    ``Expect`` is answered by the proxy itself (see _request_body), so it is
    not forwarded either.
    """
    replaced = {b"expect"}
    if route is not None:
        if route.auth_header is not None:
            replaced.add(b"authorization")
        replaced.update(name.lower().encode("latin-1") for name in route.extra_headers)
    headers = [(name, value) for name, value in _strip_hop_by_hop(request.headers.raw_items())
               if name.lower() not in replaced]
    if not any(name.lower() == b"host" for name, _ in headers):
        headers.append((b"Host", host_header))
    if route is not None:
        if route.auth_header is not None:
            headers.append((b"Authorization", route.auth_header.encode("latin-1")))
        headers.extend((name.encode("latin-1"), value.encode("latin-1"))
                       for name, value in route.extra_headers.items())
    return headers


def _has_body(request: h11.Request) -> bool:
    for name, value in request.headers:
        if name == b"transfer-encoding":
            return True
        if name == b"content-length":
            return value.strip() != b"0"
    return False


def _server_connection() -> h11.Connection:
    return h11.Connection(h11.SERVER, max_incomplete_event_size=_MAX_HEADER_BYTES)


def _next_cycle(h: h11.Connection) -> bool:
    """Start the next keep-alive cycle if both sides finished cleanly; False means close."""
    if h.our_state is h11.DONE and h.their_state is h11.DONE:
        h.start_next_cycle()
        return True
    return False


async def _respond_text(h: h11.Connection, stream: _PlainStream | _TLSStream, status: int,
                        message: str, method: bytes | None, *, close: bool = False) -> None:
    body = (message + "\n").encode("utf-8")
    headers = [(b"Content-Type", b"text/plain; charset=utf-8"),
               (b"Content-Length", str(len(body)).encode("ascii"))]
    if close:
        headers.append((b"Connection", b"close"))
    await stream.send(h.send(h11.Response(status_code=status, headers=headers)))
    if method != b"HEAD":
        await stream.send(h.send(h11.Data(data=body)))
    await stream.send(h.send(h11.EndOfMessage()))


async def _reject(h: h11.Connection, stream: _PlainStream | _TLSStream, status: int,
                  message: str, method: bytes | None = None) -> None:
    """Best-effort error response before closing; skipped if the protocol state forbids one."""
    if h.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
        return
    with contextlib.suppress(*_CONNECTION_ERRORS):
        await _respond_text(h, stream, status, message, method, close=True)


async def _read_client_hello(reader: asyncio.StreamReader) -> tuple[bytes, str | None]:
    """Read, without answering, the TLS record(s) carrying a ClientHello.

    Returns the raw bytes (to replay into a TLS session or a tunnel) and the
    SNI host name; ``(b"", None)`` if the client left without sending anything.
    """
    raw = bytearray()
    handshake = bytearray()
    while True:
        try:
            header = await reader.readexactly(5)
        except asyncio.IncompleteReadError as exc:
            if not raw and not exc.partial:
                return b"", None
            raise
        if header[0] != 0x16:
            raise _NotTLSError(f"first byte {header[0]:#04x} is not a TLS handshake record")
        body = await reader.readexactly(int.from_bytes(header[3:5]))
        raw += header + body
        handshake += body
        if len(handshake) >= 4:
            if handshake[0] != 0x01:
                raise _NotTLSError("first handshake message is not a ClientHello")
            needed = 4 + int.from_bytes(handshake[1:4])
            if needed > _MAX_CLIENT_HELLO:
                raise _NotTLSError("ClientHello is implausibly large")
            if len(handshake) >= needed:
                return bytes(raw), _parse_sni(bytes(handshake[4:needed]))
        if len(raw) > _MAX_CLIENT_HELLO:
            raise _NotTLSError("ClientHello is implausibly large")


def _parse_sni(hello: bytes) -> str | None:
    """The server_name (RFC 6066 §3) in a ClientHello body (RFC 8446 §4.1.2), if any."""
    try:
        pos = 2 + 32  # legacy_version, random
        pos += 1 + hello[pos]  # legacy_session_id
        pos += 2 + int.from_bytes(hello[pos:pos + 2])  # cipher_suites
        pos += 1 + hello[pos]  # legacy_compression_methods
        end = min(len(hello), pos + 2 + int.from_bytes(hello[pos:pos + 2]))
        pos += 2
        while pos + 4 <= end:
            ext_type = int.from_bytes(hello[pos:pos + 2])
            ext_len = int.from_bytes(hello[pos + 2:pos + 4])
            pos += 4
            if ext_type == 0:  # server_name: a list of (type, length, name)
                names = hello[pos:pos + ext_len]
                i = 2
                while i + 3 <= len(names):
                    name_type, name_len = names[i], int.from_bytes(names[i + 1:i + 3])
                    name = names[i + 3:i + 3 + name_len]
                    if name_type == 0 and len(name) == name_len:
                        return name.decode("ascii")
                    i += 3 + name_len
                return None
            pos += ext_len
    except (IndexError, UnicodeDecodeError):
        return None
    return None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled (proxy shutting down)"
    return f"{type(exc).__name__}: {exc}"
