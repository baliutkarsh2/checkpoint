"""Cross-cutting middleware: request IDs, structured access logs, rate limiting.

These are deliberately simple. checkpoint serve runs locally for one developer,
not behind a load balancer with millions of requests, so we don't need
slowapi / starlette-context / loguru — stdlib + a small token bucket suffices.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

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
