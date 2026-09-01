"""Discord MCP server — wraps `checkpoint.twins.discord` REST surface.

Tool names mirror the Discord MCP server's tool list: guild/channel/message
management, reactions, roles, webhooks. Each tool is a thin REST shim sharing
STATE with the twin.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI

from checkpoint.fake_credentials import FAKE_DISCORD_TOKEN
from checkpoint.mcp_compat import FastMCP, make_server

from ._shim import make_shim, mount_mcp_on_fastapi

DISCORD_BOOTSTRAP_TOKEN = FAKE_DISCORD_TOKEN


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Discord twin."""
    raw = os.environ.get("DISCORD_BOOTSTRAP_TOKEN", DISCORD_BOOTSTRAP_TOKEN)
    # Strip "Bot " prefix — shim adds its own auth_scheme
    token = raw[4:] if raw.startswith("Bot ") else raw
    shim = make_shim(app, token, auth_scheme="Bot")

    mcp = make_server(
        name="checkpoint-discord",
        instructions="Stateful synthetic Discord. Tool names mirror the official Discord MCP server.",
    )

    # ----- Bot / User -------------------------------------------------------

    @mcp.tool()
    async def discord_get_current_user() -> Any:
        """Get the current bot user."""
        return await shim("GET", "/api/v10/users/@me")

    @mcp.tool()
    async def discord_get_user(user_id: str) -> Any:
        """Get a Discord user by ID."""
        return await shim("GET", f"/api/v10/users/{user_id}")

    # ----- Guild ------------------------------------------------------------

    @mcp.tool()
    async def discord_get_guild(guild_id: str) -> Any:
        """Get a guild (server) by ID."""
        return await shim("GET", f"/api/v10/guilds/{guild_id}")

    # ----- Channels ---------------------------------------------------------

    @mcp.tool()
    async def discord_list_guild_channels(guild_id: str) -> Any:
        """List all channels in a guild."""
        return await shim("GET", f"/api/v10/guilds/{guild_id}/channels")

    @mcp.tool()
    async def discord_create_channel(
        guild_id: str,
        name: str,
        channel_type: int = 0,
        topic: str | None = None,
        parent_id: str | None = None,
        position: int | None = None,
        nsfw: bool = False,
    ) -> Any:
        """Create a channel in a guild.

        channel_type: 0=text, 2=voice, 4=category, 15=forum.
        """
        body: dict[str, Any] = {"name": name, "type": channel_type, "nsfw": nsfw}
        if topic is not None:
            body["topic"] = topic
        if parent_id is not None:
            body["parent_id"] = parent_id
        if position is not None:
            body["position"] = position
        return await shim("POST", f"/api/v10/guilds/{guild_id}/channels", json=body)

    @mcp.tool()
    async def discord_get_channel(channel_id: str) -> Any:
        """Get a channel by ID."""
        return await shim("GET", f"/api/v10/channels/{channel_id}")

    @mcp.tool()
    async def discord_modify_channel(
        channel_id: str,
        name: str | None = None,
        topic: str | None = None,
        nsfw: bool | None = None,
        position: int | None = None,
        parent_id: str | None = None,
        rate_limit_per_user: int | None = None,
    ) -> Any:
        """Modify a channel's settings."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if topic is not None:
            body["topic"] = topic
        if nsfw is not None:
            body["nsfw"] = nsfw
        if position is not None:
            body["position"] = position
        if parent_id is not None:
            body["parent_id"] = parent_id
        if rate_limit_per_user is not None:
            body["rate_limit_per_user"] = rate_limit_per_user
        return await shim("PATCH", f"/api/v10/channels/{channel_id}", json=body)

    @mcp.tool()
    async def discord_delete_channel(channel_id: str) -> Any:
        """Delete a channel."""
        return await shim("DELETE", f"/api/v10/channels/{channel_id}")

    # ----- Messages ---------------------------------------------------------

    @mcp.tool()
    async def discord_get_messages(
        channel_id: str,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> Any:
        """Get messages from a channel.

        limit: max messages to return (1–100).
        before/after: message IDs to paginate around.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return await shim("GET", f"/api/v10/channels/{channel_id}/messages", params=params)

    @mcp.tool()
    async def discord_send_message(
        channel_id: str,
        content: str,
        tts: bool = False,
        embeds: list[dict] | None = None,
        message_reference: dict | None = None,
    ) -> Any:
        """Send a message to a channel.

        message_reference: use {"message_id": "..."} to reply to a message.
        """
        body: dict[str, Any] = {"content": content, "tts": tts}
        if embeds is not None:
            body["embeds"] = embeds
        if message_reference is not None:
            body["message_reference"] = message_reference
        return await shim("POST", f"/api/v10/channels/{channel_id}/messages", json=body)

    @mcp.tool()
    async def discord_edit_message(
        channel_id: str,
        message_id: str,
        content: str | None = None,
        embeds: list[dict] | None = None,
    ) -> Any:
        """Edit a message."""
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embeds is not None:
            body["embeds"] = embeds
        return await shim("PATCH", f"/api/v10/channels/{channel_id}/messages/{message_id}", json=body)

    @mcp.tool()
    async def discord_delete_message(channel_id: str, message_id: str) -> Any:
        """Delete a message."""
        return await shim("DELETE", f"/api/v10/channels/{channel_id}/messages/{message_id}")

    @mcp.tool()
    async def discord_bulk_delete_messages(channel_id: str, message_ids: list[str]) -> Any:
        """Bulk delete messages (2–100 messages at once)."""
        return await shim(
            "POST",
            f"/api/v10/channels/{channel_id}/messages/bulk-delete",
            json={"messages": message_ids},
        )

    # ----- Reactions --------------------------------------------------------

    @mcp.tool()
    async def discord_add_reaction(channel_id: str, message_id: str, emoji: str) -> Any:
        """Add a reaction to a message.

        emoji: URL-encoded emoji name (e.g. "👍" or "thumbsup").
        """
        return await shim(
            "PUT",
            f"/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
        )

    @mcp.tool()
    async def discord_remove_reaction(channel_id: str, message_id: str, emoji: str) -> Any:
        """Remove the bot's reaction from a message."""
        return await shim(
            "DELETE",
            f"/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
        )

    @mcp.tool()
    async def discord_get_reactions(channel_id: str, message_id: str, emoji: str) -> Any:
        """Get users who reacted with a specific emoji."""
        return await shim(
            "GET",
            f"/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
        )

    # ----- Pins -------------------------------------------------------------

    @mcp.tool()
    async def discord_get_pinned_messages(channel_id: str) -> Any:
        """Get pinned messages in a channel."""
        return await shim("GET", f"/api/v10/channels/{channel_id}/pins")

    @mcp.tool()
    async def discord_pin_message(channel_id: str, message_id: str) -> Any:
        """Pin a message in a channel."""
        return await shim("PUT", f"/api/v10/channels/{channel_id}/pins/{message_id}")

    @mcp.tool()
    async def discord_unpin_message(channel_id: str, message_id: str) -> Any:
        """Unpin a message from a channel."""
        return await shim("DELETE", f"/api/v10/channels/{channel_id}/pins/{message_id}")

    # ----- Members ----------------------------------------------------------

    @mcp.tool()
    async def discord_list_guild_members(guild_id: str, limit: int = 100) -> Any:
        """List members of a guild."""
        return await shim("GET", f"/api/v10/guilds/{guild_id}/members", params={"limit": limit})

    @mcp.tool()
    async def discord_get_guild_member(guild_id: str, user_id: str) -> Any:
        """Get a specific member of a guild."""
        return await shim("GET", f"/api/v10/guilds/{guild_id}/members/{user_id}")

    @mcp.tool()
    async def discord_assign_role(guild_id: str, user_id: str, role_id: str) -> Any:
        """Assign a role to a guild member."""
        return await shim("PUT", f"/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}")

    @mcp.tool()
    async def discord_remove_role(guild_id: str, user_id: str, role_id: str) -> Any:
        """Remove a role from a guild member."""
        return await shim("DELETE", f"/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}")

    # ----- Roles ------------------------------------------------------------

    @mcp.tool()
    async def discord_list_roles(guild_id: str) -> Any:
        """List all roles in a guild."""
        return await shim("GET", f"/api/v10/guilds/{guild_id}/roles")

    @mcp.tool()
    async def discord_create_role(
        guild_id: str,
        name: str,
        color: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
        permissions: str | None = None,
    ) -> Any:
        """Create a role in a guild."""
        body: dict[str, Any] = {
            "name": name,
            "color": color,
            "hoist": hoist,
            "mentionable": mentionable,
        }
        if permissions is not None:
            body["permissions"] = permissions
        return await shim("POST", f"/api/v10/guilds/{guild_id}/roles", json=body)

    @mcp.tool()
    async def discord_modify_role(
        guild_id: str,
        role_id: str,
        name: str | None = None,
        color: int | None = None,
        hoist: bool | None = None,
        mentionable: bool | None = None,
        permissions: str | None = None,
    ) -> Any:
        """Modify a guild role."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if color is not None:
            body["color"] = color
        if hoist is not None:
            body["hoist"] = hoist
        if mentionable is not None:
            body["mentionable"] = mentionable
        if permissions is not None:
            body["permissions"] = permissions
        return await shim("PATCH", f"/api/v10/guilds/{guild_id}/roles/{role_id}", json=body)

    @mcp.tool()
    async def discord_delete_role(guild_id: str, role_id: str) -> Any:
        """Delete a role from a guild."""
        return await shim("DELETE", f"/api/v10/guilds/{guild_id}/roles/{role_id}")

    # ----- Webhooks ---------------------------------------------------------

    @mcp.tool()
    async def discord_create_webhook(
        channel_id: str,
        name: str,
        avatar: str | None = None,
    ) -> Any:
        """Create a webhook for a channel."""
        body: dict[str, Any] = {"name": name}
        if avatar is not None:
            body["avatar"] = avatar
        return await shim("POST", f"/api/v10/channels/{channel_id}/webhooks", json=body)

    @mcp.tool()
    async def discord_list_channel_webhooks(channel_id: str) -> Any:
        """List webhooks for a channel."""
        return await shim("GET", f"/api/v10/channels/{channel_id}/webhooks")

    @mcp.tool()
    async def discord_execute_webhook(
        webhook_id: str,
        webhook_token: str,
        content: str,
        embeds: list[dict] | None = None,
        username: str | None = None,
    ) -> Any:
        """Execute (send a message via) a webhook."""
        body: dict[str, Any] = {"content": content}
        if embeds is not None:
            body["embeds"] = embeds
        if username is not None:
            body["username"] = username
        return await shim(
            "POST",
            f"/api/v10/webhooks/{webhook_id}/{webhook_token}",
            json=body,
        )

    @mcp.tool()
    async def discord_delete_webhook(webhook_id: str) -> Any:
        """Delete a webhook."""
        return await shim("DELETE", f"/api/v10/webhooks/{webhook_id}")

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Discord FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
