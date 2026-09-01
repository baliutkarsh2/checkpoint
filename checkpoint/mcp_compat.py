"""Compatibility across the mcp SDK's 1.x and 2.x majors.

mcp 2.0 made two changes that affect us (see the official migration guide,
https://py.sdk.modelcontextprotocol.io/v2/migration/):

1. ``FastMCP`` was renamed to ``MCPServer`` and moved from
   ``mcp.server.fastmcp`` to ``mcp.server.mcpserver``.
2. Transport options — ``stateless_http``, ``streamable_http_path`` — moved off
   the constructor and onto the transport methods (``run()``,
   ``streamable_http_app()``, ``sse_app()``).

Everything else we use is unchanged: the ``@server.tool()`` decorator keeps the
same arguments and handler signatures, and ``name`` / ``instructions`` remain
constructor arguments.

We deliberately do NOT cap ``mcp`` in pyproject. An upper bound in a *published
library* propagates into every downstream resolver and would make this package
uninstallable alongside a newer SDK — the cost of a speculative cap lands on
users, not on us. Supporting both majors is the correct fix, and CI runs the
suite against whatever the resolver actually picks, so a future breaking major
fails the build instead of silently reaching anyone.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP

    MCP_MAJOR = 2
except ModuleNotFoundError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP  # noqa: F401

    MCP_MAJOR = 1

__all__ = ["FastMCP", "MCP_MAJOR", "client_streams", "make_server", "streamable_http_app"]


def make_server(
    name: str,
    instructions: str | None = None,
    *,
    stateless_http: bool = True,
    streamable_http_path: str = "/",
) -> Any:
    """Build a server, putting transport options where this major expects them."""
    if MCP_MAJOR >= 2:
        # 2.x takes transport options on the app/run methods instead; they are
        # applied by streamable_http_app() below.
        server = FastMCP(name=name, instructions=instructions)
        server._checkpoint_transport = {
            "stateless_http": stateless_http,
            "streamable_http_path": streamable_http_path,
        }
        return server
    return FastMCP(
        name=name,
        instructions=instructions,
        stateless_http=stateless_http,
        streamable_http_path=streamable_http_path,
    )


def streamable_http_app(server: Any) -> Any:
    """Return the ASGI app, forwarding transport options on 2.x."""
    if MCP_MAJOR >= 2:
        opts = getattr(server, "_checkpoint_transport", {})
        return server.streamable_http_app(**opts)
    return server.streamable_http_app()


@asynccontextmanager
async def client_streams(url: str) -> Any:
    """Open a streamable-HTTP client, yielding ``(read, write, get_session_id)``.

    2.0 renamed ``streamablehttp_client`` to ``streamable_http_client`` and
    changed what it yields from a 3-tuple to ``(read, write)``. This normalises
    both to the 3-tuple shape; ``get_session_id`` is ``None`` on 2.x.
    """
    if MCP_MAJOR >= 2:
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(url) as streams:
            read, write = streams[0], streams[1]
            yield read, write, None
    else:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url) as (read, write, get_session_id):
            yield read, write, get_session_id
