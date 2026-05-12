"""REST shim used by every MCP server in this package.

Each MCP tool body is a one-liner that calls into one of these shims:
the shim runs the in-process FastAPI app via `httpx.ASGITransport`,
which means MCP and REST share the same `STATE` dict with zero network
hop. Authentication is handled here — the shim stamps the twin's
bootstrap token onto every request so callers don't need to know about
it.

`mount_mcp_on_fastapi(app, mcp, path)` mounts a FastMCP instance at
`path` on a FastAPI app and chains the MCP session manager's lifespan
into the FastAPI lifespan (without it, the mounted MCP returns 500
"Task group is not initialized").
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Callable, Awaitable, Mapping

import httpx
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP


ShimFn = Callable[..., Awaitable[Any]]


def make_shim(
    app: FastAPI,
    bootstrap_token: str,
    *,
    auth_header: str = "Authorization",
    auth_scheme: str = "token",
) -> ShimFn:
    """Build a REST shim bound to one FastAPI twin app.

    Returns an async callable `shim(method, path, *, json=None,
    params=None, form=None, extra_headers=None)` that returns the parsed
    JSON body of the response. Non-2xx responses are returned as a dict
    with an injected `_status` field — letting MCP tool callers surface
    real API error envelopes to the agent rather than raising.
    """
    transport = httpx.ASGITransport(app=app)
    auth_value = (
        f"{auth_scheme} {bootstrap_token}" if auth_scheme else bootstrap_token
    )

    async def shim(
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        headers: dict[str, str] = {auth_header: auth_value}
        if extra_headers:
            headers.update(extra_headers)
        # httpx mutual-exclusion: json vs data. Form takes precedence
        # because Stripe's twin reads `await request.form()`.
        kwargs: dict[str, Any] = {"headers": headers}
        if form is not None:
            kwargs["data"] = {k: v for k, v in form.items() if v is not None}
        elif json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}

        async with httpx.AsyncClient(
            transport=transport, base_url="http://twin"
        ) as client:
            resp = await client.request(method, path, **kwargs)

        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text}
        if resp.status_code >= 400 and isinstance(body, dict):
            body = {**body, "_status": resp.status_code}
        elif resp.status_code >= 400:
            body = {"_status": resp.status_code, "_body": body}
        return body

    return shim


def mount_mcp_on_fastapi(app: FastAPI, mcp: FastMCP, path: str = "/mcp") -> None:
    """Mount the MCP streamable-HTTP app at `path` on a FastAPI app.

    Calls `mcp.streamable_http_app()` (which lazy-creates the session
    manager), mounts it, and chains the session manager's `run()` into
    the FastAPI app's lifespan so the manager's task group is alive
    while requests are served. Without the lifespan chain, MCP returns
    500 "Task group is not initialized" on the first request.
    """
    sub_app = mcp.streamable_http_app()
    app.mount(path, sub_app)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _chained_lifespan(_app: FastAPI):
        async with previous_lifespan(_app):
            async with mcp.session_manager.run():
                yield

    app.router.lifespan_context = _chained_lifespan
