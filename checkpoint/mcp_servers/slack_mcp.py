"""Slack MCP server — wraps `checkpoint.twins.slack` REST surface.

Tool names match Archal's faithful list (SCOPE.md §3.4): 8 tools across
chat, conversations, reactions, and users. Each tool body is a thin
REST shim — REST and MCP share one `STATE` dict.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from ._shim import make_shim, mount_mcp_on_fastapi


SLACK_BOOTSTRAP_TOKEN = "xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx"


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Slack twin."""
    token = os.environ.get("SLACK_BOOTSTRAP_TOKEN", SLACK_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = FastMCP(
        name="checkpoint-slack",
        instructions="Stateful synthetic Slack. Tool names match Archal §3.4.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ----- chat ---------------------------------------------------------

    @mcp.tool()
    async def slack_post_message(channel: str, text: str) -> Any:
        """Post a message to a channel (id or name)."""
        return await shim(
            "POST", "/api/chat.postMessage",
            json={"channel": channel, "text": text},
        )

    @mcp.tool()
    async def slack_reply_to_thread(
        channel: str, thread_ts: str, text: str
    ) -> Any:
        """Reply to a thread (parent message ts)."""
        return await shim(
            "POST", "/api/chat.postMessage",
            json={"channel": channel, "text": text, "thread_ts": thread_ts},
        )

    # ----- conversations -----------------------------------------------

    @mcp.tool()
    async def slack_get_channel_history(
        channel: str, limit: int = 100, cursor: str = ""
    ) -> Any:
        """Get top-level messages in a channel (paged)."""
        return await shim(
            "GET", "/api/conversations.history",
            params={"channel": channel, "limit": limit, "cursor": cursor},
        )

    @mcp.tool()
    async def slack_get_thread_replies(channel: str, ts: str) -> Any:
        """Get all replies in a thread."""
        return await shim(
            "GET", "/api/conversations.replies",
            params={"channel": channel, "ts": ts},
        )

    @mcp.tool()
    async def slack_list_channels(
        cursor: str = "", limit: int = 100, types: str = "public_channel"
    ) -> Any:
        """List channels (cursor-paginated)."""
        return await shim(
            "GET", "/api/conversations.list",
            params={"cursor": cursor, "limit": limit, "types": types},
        )

    # ----- reactions ----------------------------------------------------

    @mcp.tool()
    async def slack_add_reaction(
        channel: str, timestamp: str, name: str
    ) -> Any:
        """Add an emoji reaction to a message."""
        return await shim(
            "POST", "/api/reactions.add",
            json={"channel": channel, "timestamp": timestamp, "name": name},
        )

    # ----- users --------------------------------------------------------

    @mcp.tool()
    async def slack_get_users(cursor: str = "", limit: int = 100) -> Any:
        """List workspace members (cursor-paginated)."""
        return await shim(
            "GET", "/api/users.list",
            params={"cursor": cursor, "limit": limit},
        )

    @mcp.tool()
    async def slack_get_user_profile(user: str) -> Any:
        """Get a single user's profile by id or name."""
        return await shim(
            "GET", "/api/users.profile.get", params={"user": user},
        )

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Slack FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
