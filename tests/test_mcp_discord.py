"""Discord MCP server end-to-end tests.

Boots the Discord twin via uvicorn, verifies the tool surface, and exercises
key tools — confirming mutations are visible via REST /_state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest


DISCORD_TOOL_NAMES = {
    "discord_get_current_user",
    "discord_get_user",
    "discord_get_guild",
    "discord_list_guild_channels",
    "discord_create_channel",
    "discord_get_channel",
    "discord_modify_channel",
    "discord_delete_channel",
    "discord_get_messages",
    "discord_send_message",
    "discord_edit_message",
    "discord_delete_message",
    "discord_bulk_delete_messages",
    "discord_add_reaction",
    "discord_remove_reaction",
    "discord_get_reactions",
    "discord_get_pinned_messages",
    "discord_pin_message",
    "discord_unpin_message",
    "discord_list_guild_members",
    "discord_get_guild_member",
    "discord_assign_role",
    "discord_remove_role",
    "discord_list_roles",
    "discord_create_role",
    "discord_modify_role",
    "discord_delete_role",
    "discord_create_webhook",
    "discord_list_channel_webhooks",
    "discord_execute_webhook",
    "discord_delete_webhook",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/_health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


@pytest.fixture
def discord_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.discord:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"Discord twin failed to start on port {port}"
        httpx.post(f"http://127.0.0.1:{port}/_seed/small-server").raise_for_status()
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_discord_mcp_lists_expected_tools(discord_twin):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{discord_twin}/mcp/"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert DISCORD_TOOL_NAMES.issubset(names), (
                f"missing tools: {DISCORD_TOOL_NAMES - names}"
            )


@pytest.mark.asyncio
async def test_discord_mcp_key_tools(discord_twin):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{discord_twin}/mcp/"
    state_url = f"http://127.0.0.1:{discord_twin}/_state"

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Get state to find seeded guild + channel
            state = httpx.get(state_url).json()
            assert state["guilds"], "small-server seed should have a guild"
            guild_id = next(iter(state["guilds"].keys()))
            assert state["channels"], "small-server seed should have channels"
            channel_id = next(iter(state["channels"].keys()))

            # 1. get_current_user
            result = await session.call_tool("discord_get_current_user", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "id" in text

            # 2. get_guild
            result = await session.call_tool("discord_get_guild", {"guild_id": guild_id})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert guild_id in text or "name" in text

            # 3. list_guild_channels
            result = await session.call_tool("discord_list_guild_channels", {"guild_id": guild_id})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert text  # any non-empty response

            # 4. send_message
            result = await session.call_tool("discord_send_message", {
                "channel_id": channel_id,
                "content": "Hello from MCP test!",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "Hello from MCP test!" in text

            state = httpx.get(state_url).json()
            msgs = state["messages"].get(channel_id, [])
            assert any(m["content"] == "Hello from MCP test!" for m in msgs)

            # Get the message id for further tests
            msg_id = next(
                m["id"] for m in msgs if m["content"] == "Hello from MCP test!"
            )

            # 5. add_reaction
            result = await session.call_tool("discord_add_reaction", {
                "channel_id": channel_id,
                "message_id": msg_id,
                "emoji": "👍",
            })
            assert result  # 204 no content

            # 6. list_roles
            result = await session.call_tool("discord_list_roles", {"guild_id": guild_id})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert text

            # 7. create_webhook
            result = await session.call_tool("discord_create_webhook", {
                "channel_id": channel_id,
                "name": "mcp-alerts",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp-alerts" in text

            state = httpx.get(state_url).json()
            assert any(
                wh["name"] == "mcp-alerts"
                for wh in state["webhooks"].values()
            )

            # 8. get_messages
            result = await session.call_tool("discord_get_messages", {
                "channel_id": channel_id,
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "Hello from MCP test!" in text
