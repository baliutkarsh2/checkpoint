"""Phase 6 Plan 02: Slack MCP server end-to-end through the official client.

Boots the Slack twin via uvicorn, opens an MCP client at /mcp, verifies
the 8-tool surface from SCOPE §3.4 and exercises every tool against
the `engineering-team` seed (the default Slack seed) — confirming each
mutation is visible to the REST /_state view.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

SLACK_TOOL_NAMES = {
    "slack_post_message",
    "slack_reply_to_thread",
    "slack_get_channel_history",
    "slack_get_thread_replies",
    "slack_list_channels",
    "slack_add_reaction",
    "slack_get_users",
    "slack_get_user_profile",
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
def slack_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.slack:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"twin failed to start on port {port}"
        # Load the engineering-team seed for richer state.
        httpx.post(f"http://127.0.0.1:{port}/_seed/engineering-team").raise_for_status()
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_slack_mcp_lists_archal_tool_set(slack_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{slack_twin}/mcp/"
    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == SLACK_TOOL_NAMES, (
                f"missing: {SLACK_TOOL_NAMES - names}, "
                f"extra: {names - SLACK_TOOL_NAMES}"
            )


@pytest.mark.asyncio
async def test_slack_mcp_all_eight_tools_callable(slack_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{slack_twin}/mcp/"
    state_url = f"http://127.0.0.1:{slack_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_channels → make sure the seed populated.
            chans = await session.call_tool("slack_list_channels", {})
            chans_text = "".join(getattr(c, "text", "") for c in chans.content)
            assert "general" in chans_text or "engineering" in chans_text

            # Pick the first channel id from /_state for deterministic ops.
            state = httpx.get(state_url).json()
            assert state["channels"], "engineering-team seed should ship channels"
            chan_id = next(iter(state["channels"].keys()))

            # 2. post_message
            r1 = await session.call_tool(
                "slack_post_message",
                {"channel": chan_id, "text": "hello from MCP"},
            )
            txt1 = "".join(getattr(c, "text", "") for c in r1.content)
            assert "hello from MCP" in txt1
            state = httpx.get(state_url).json()
            assert any(
                m["text"] == "hello from MCP"
                for m in state["messages"].get(chan_id, [])
            )

            # Extract the parent ts for the reply / reaction tests.
            parent_ts = next(
                m["ts"] for m in state["messages"][chan_id]
                if m["text"] == "hello from MCP"
            )

            # 3. reply_to_thread
            await session.call_tool(
                "slack_reply_to_thread",
                {"channel": chan_id, "thread_ts": parent_ts, "text": "thread reply"},
            )
            state = httpx.get(state_url).json()
            assert any(
                m.get("thread_ts") == parent_ts and m["text"] == "thread reply"
                for m in state["messages"][chan_id]
            )

            # 4. get_channel_history
            hist = await session.call_tool(
                "slack_get_channel_history", {"channel": chan_id, "limit": 50},
            )
            hist_txt = "".join(getattr(c, "text", "") for c in hist.content)
            assert "hello from MCP" in hist_txt

            # 5. get_thread_replies
            replies = await session.call_tool(
                "slack_get_thread_replies", {"channel": chan_id, "ts": parent_ts},
            )
            replies_txt = "".join(getattr(c, "text", "") for c in replies.content)
            assert "thread reply" in replies_txt

            # 6. add_reaction
            await session.call_tool(
                "slack_add_reaction",
                {"channel": chan_id, "timestamp": parent_ts, "name": "rocket"},
            )
            state = httpx.get(state_url).json()
            parent_msg = next(
                m for m in state["messages"][chan_id] if m["ts"] == parent_ts
            )
            assert any(r["name"] == "rocket" for r in parent_msg.get("reactions", []))

            # 7. get_users
            users = await session.call_tool(
                "slack_get_users", {"limit": 10},
            )
            users_txt = "".join(getattr(c, "text", "") for c in users.content)
            assert "members" in users_txt

            # 8. get_user_profile
            # Pick a user from state.
            uid = next(iter(state["users"].keys()))
            prof = await session.call_tool(
                "slack_get_user_profile", {"user": uid},
            )
            prof_txt = "".join(getattr(c, "text", "") for c in prof.content)
            assert "profile" in prof_txt
