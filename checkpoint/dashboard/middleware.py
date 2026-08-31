"""Cross-cutting middleware: request IDs, structured access logs, rate
limiting, optional bearer-token auth, optional read-only mode.

Local default: no auth, no read-only — `checkpoint serve` works for a single
developer with zero config.

Cloud default (any time CHECKPOINT_DASHBOARD_API_KEY is set): every /api/*
write requires `Authorization: Bearer <key>`; reads are unauthenticated unless
CHECKPOINT_DASHBOARD_AUTH_READS=1 is also set. CHECKPOINT_DASHBOARD_READ_ONLY=1
disables `POST /api/jobs` entirely (no spawning subprocesses from the network).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger("checkpoint.dashboard.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns each request an x-request-id header (in + out)."""

    HEADER = "x-request-id"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex[:12]
        # Make it accessible to handlers via request.state.
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[self.HEADER] = rid
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Structured single-line access logs.

    We log everything except SSE streams (they'd produce one log line per
    second forever). We also skip /healthz (uptime probes are noise).
    """

    SKIP_PATHS = {"/healthz", "/metrics"}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if path in self.SKIP_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        rid = getattr(request.state, "request_id", "—")
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            log.exception(
                "rid=%s %s %s -> 500 (%.1fms) [unhandled]",
                rid,
                request.method,
                path,
                duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        log.info(
            "rid=%s %s %s -> %d (%.1fms)",
            rid,
            request.method,
            path,
            response.status_code,
            duration_ms,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-client-IP rate limit on /api/* writes.

    Reads are unlimited. Writes (POST/PUT/PATCH/DELETE) are capped at
    `max_writes` per `window_s` per remote IP. This protects against an
    accidental loop in client code that triggers `New run` 10k times.
    """

    def __init__(
        self,
        app: Callable,
        *,
        max_writes: int = 30,
        window_s: float = 10.0,
    ) -> None:
        super().__init__(app)
        self.max_writes = max_writes
        self.window_s = window_s
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = self._buckets[client]
        cutoff = now - self.window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_writes:
            retry = self.window_s - (now - bucket[0])
            return JSONResponse(
                {"error": "rate limited", "retry_after_s": round(retry, 2)},
                status_code=429,
                headers={"retry-after": str(int(retry) + 1)},
            )
        bucket.append(now)
        return await call_next(request)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Optional bearer-token auth for shared / cloud-hosted dashboards.

    No-op when CHECKPOINT_DASHBOARD_API_KEY is unset — keeps the local-dev UX
    one-command. When set:
      - every /api/* write (POST/PUT/PATCH/DELETE) requires
        ``Authorization: Bearer <key>`` matching the env var
      - reads stay open unless CHECKPOINT_DASHBOARD_AUTH_READS=1 is also set
        (so you can still embed the SPA in an internal status page)
      - /healthz, /metrics, /api/docs, /api/openapi.json, and the SPA static
        assets are always public (load balancers + bookmarks need them)
    """

    PUBLIC_PATHS = {"/healthz", "/metrics", "/api/docs", "/api/redoc", "/api/openapi.json"}

    def __init__(self, app: Callable) -> None:
        super().__init__(app)
        self._key = os.environ.get("CHECKPOINT_DASHBOARD_API_KEY", "").strip()
        self._auth_reads = os.environ.get("CHECKPOINT_DASHBOARD_AUTH_READS") == "1"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self._key:
            return await call_next(request)
        path = request.url.path
        if path in self.PUBLIC_PATHS:
            return await call_next(request)
        # SPA static (anything not /api/*) stays public so the browser can
        # load JS/CSS without a token; the SPA itself attaches the token to
        # XHR via a `?key=` query param or Authorization header.
        if not path.startswith("/api/"):
            return await call_next(request)
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        if not is_write and not self._auth_reads:
            return await call_next(request)
        if not self._token_ok(request):
            return JSONResponse(
                {"error": "missing or invalid bearer token",
                 "hint": "set CHECKPOINT_DASHBOARD_API_KEY on the server "
                         "and pass `Authorization: Bearer <key>` from the client."},
                status_code=401,
                headers={"www-authenticate": "Bearer"},
            )
        return await call_next(request)

    def _token_ok(self, request: Request) -> bool:
        # Constant-time comparison to defeat timing attacks on a per-byte basis.
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            presented = header.split(None, 1)[1].strip()
            if secrets.compare_digest(presented, self._key):
                return True
        # Fallback: ?key=<token> (handy for SSE / EventSource which can't set headers).
        query_key = request.query_params.get("key", "")
        if query_key and secrets.compare_digest(query_key, self._key):
            return True
        return False


class ReadOnlyJobsMiddleware(BaseHTTPMiddleware):
    """When CHECKPOINT_DASHBOARD_READ_ONLY=1, block job creation entirely.

    Useful for sharing a hosted dashboard with viewers who should be able to
    browse runs but never spawn `checkpoint run` subprocesses on the server.
    Returns 403 with a clear hint instead of silently 404-ing.
    """

    def __init__(self, app: Callable) -> None:
        super().__init__(app)
        self._enabled = os.environ.get("CHECKPOINT_DASHBOARD_READ_ONLY") == "1"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self._enabled:
            return await call_next(request)
        if request.url.path.startswith("/api/jobs") and request.method != "GET":
            return JSONResponse(
                {"error": "read-only mode",
                 "hint": "this dashboard was started with CHECKPOINT_DASHBOARD_READ_ONLY=1; "
                         "job creation/cancellation is disabled."},
                status_code=403,
            )
        return await call_next(request)
