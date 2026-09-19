"""Helpers shared by the intercept-proxy tests (fixtures live in conftest.py)."""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route as StarletteRoute

from checkpoint.proxy.server import InterceptProxy, ProxyEvent

BLOCK = bytes(range(256)) * 256  # 64 KiB of a recognisable pattern


def pattern(n: int) -> bytes:
    return (BLOCK * (n // len(BLOCK) + 1))[:n]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> object:
    """Poll until ``predicate()`` is truthy (events are recorded when a connection ends)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met within the timeout")


def recorded(proxy: InterceptProxy, **fields: object) -> list[ProxyEvent]:
    """The proxy's events matching ``fields``, waiting until there is at least one.

    An event is recorded when the proxy finishes with a request or tunnel,
    which can be a moment after the client has already read the response.
    """
    def matches() -> list[ProxyEvent]:
        return [e for e in proxy.events() if all(getattr(e, k) == v for k, v in fields.items())]

    return wait_for(matches)  # type: ignore[return-value]


# -- the echo upstream ------------------------------------------------------------------


async def _echo(request: Request) -> Response:
    body = await request.body()
    return JSONResponse({
        "method": request.method,
        "raw_path": request.scope["raw_path"].decode("latin-1"),
        "query": request.scope["query_string"].decode("latin-1"),
        "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in request.scope["headers"]],
        "body_len": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    })


async def _bytes(request: Request) -> Response:
    """``n`` pattern bytes, streamed without a Content-Length (so chunked on the wire)."""
    n = request.path_params["n"]

    async def body():
        for offset in range(0, n, len(BLOCK)):
            yield BLOCK[: min(len(BLOCK), n - offset)]

    return StreamingResponse(body(), media_type="application/octet-stream")


async def _drip(request: Request) -> Response:
    async def body():
        yield b"first\n"
        await asyncio.sleep(1.0)
        yield b"second\n"

    return StreamingResponse(body(), media_type="text/plain")


async def _sized(request: Request) -> Response:
    return Response(b"x" * 1234, media_type="text/plain")


async def _no_content(request: Request) -> Response:
    return Response(status_code=204)


async def _not_modified(request: Request) -> Response:
    return Response(status_code=304, headers={"ETag": '"v1"'})


async def _slow(request: Request) -> Response:
    await asyncio.sleep(float(request.query_params.get("s", "2")))
    return Response(b"late")


ECHO_APP = Starlette(routes=[
    StarletteRoute("/bytes/{n:int}", _bytes),
    StarletteRoute("/drip", _drip),
    StarletteRoute("/sized", _sized, methods=["GET", "HEAD"]),
    StarletteRoute("/no-content", _no_content, methods=["GET", "DELETE"]),
    StarletteRoute("/not-modified", _not_modified),
    StarletteRoute("/slow", _slow),
    StarletteRoute("/{path:path}", _echo, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
])


class ThreadedUvicorn:
    """Serve an ASGI app on a pre-bound loopback socket from a background thread."""

    def __init__(self, app: Starlette) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, http="h11", lifespan="off",
                                                     log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._sock]},
                                        daemon=True)

    def __enter__(self) -> ThreadedUvicorn:
        self._thread.start()
        wait_for(lambda: self._server.started, timeout=10)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()


@dataclass
class TLSEcho:
    port: int
    cert_path: Path
    cert_der: bytes

    def client_context(self) -> ssl.SSLContext:
        """Trusts ONLY this server's self-signed certificate — never Checkpoint's CA."""
        return ssl.create_default_context(cafile=str(self.cert_path))


# -- raw-socket clients, for what high-level clients hide ----------------------------------


def read_head(sock: socket.socket) -> bytes:
    """Read one HTTP response head (up to the blank line), leaving the rest unread."""
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        head += chunk
    return head


def read_response(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read one Content-Length-framed response: ``(head, body)``."""
    head = read_head(sock)
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return head, body


def open_tunnel(proxy_port: int, host: str, context: ssl.SSLContext,
                port: int = 443) -> ssl.SSLSocket:
    """CONNECT through the proxy and start TLS for ``host``, as a real client would."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = read_head(sock)
    assert head.startswith(b"HTTP/1.1 200"), head
    return context.wrap_socket(sock, server_hostname=host)


def https_on(sock: ssl.SSLSocket, host: str) -> http.client.HTTPSConnection:
    """An http.client connection bound to an already-open TLS socket (so it can never redial)."""
    conn = http.client.HTTPSConnection(host, timeout=10)
    conn.sock = sock
    return conn
