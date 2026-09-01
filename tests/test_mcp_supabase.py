"""Supabase MCP server end-to-end tests.

Boots the Supabase twin via uvicorn, verifies the tool surface, and exercises
key tools — confirming mutations are visible via REST /_state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

SUPABASE_TOOL_NAMES = {
    "supabase_list_tables",
    "supabase_query",
    "supabase_insert",
    "supabase_update",
    "supabase_delete",
    "supabase_upsert",
    "supabase_rpc",
    "supabase_list_auth_users",
    "supabase_get_auth_user",
    "supabase_create_auth_user",
    "supabase_update_auth_user",
    "supabase_delete_auth_user",
    "supabase_list_buckets",
    "supabase_get_bucket",
    "supabase_create_bucket",
    "supabase_update_bucket",
    "supabase_delete_bucket",
    "supabase_empty_bucket",
    "supabase_list_objects",
    "supabase_get_object_info",
    "supabase_upload_object",
    "supabase_move_object",
    "supabase_copy_object",
    "supabase_delete_object",
    "supabase_create_signed_url",
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
def supabase_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.supabase:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"Supabase twin failed to start on port {port}"
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_supabase_mcp_lists_expected_tools(supabase_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{supabase_twin}/mcp/"
    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert SUPABASE_TOOL_NAMES.issubset(names), (
                f"missing tools: {SUPABASE_TOOL_NAMES - names}"
            )


@pytest.mark.asyncio
async def test_supabase_mcp_key_tools(supabase_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{supabase_twin}/mcp/"
    state_url = f"http://127.0.0.1:{supabase_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_tables (empty)
            result = await session.call_tool("supabase_list_tables", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert text is not None

            # 2. insert a row
            result = await session.call_tool("supabase_insert", {
                "table": "tasks",
                "data": {"title": "MCP task", "done": False},
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert text  # any response

            # Verify state mutation
            state = httpx.get(state_url).json()
            assert "tasks" in state["tables"]
            rows = state["tables"]["tasks"]["rows"]
            assert any(r.get("title") == "MCP task" for r in rows)

            # 3. query
            result = await session.call_tool("supabase_query", {
                "table": "tasks",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "MCP task" in text

            # 4. create_auth_user
            result = await session.call_tool("supabase_create_auth_user", {
                "email": "mcp@test.com",
                "password": "pass1234",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp@test.com" in text

            state = httpx.get(state_url).json()
            assert any(
                u["email"] == "mcp@test.com"
                for u in state["auth_users"].values()
            )

            # 5. create_bucket
            result = await session.call_tool("supabase_create_bucket", {
                "bucket_id": "test-bucket",
                "name": "test-bucket",
                "public": True,
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert text

            state = httpx.get(state_url).json()
            assert "test-bucket" in state["storage"]["buckets"]

            # 6. list_buckets
            result = await session.call_tool("supabase_list_buckets", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "test-bucket" in text

            # 7. list_auth_users
            result = await session.call_tool("supabase_list_auth_users", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp@test.com" in text
