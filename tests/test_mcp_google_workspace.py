"""Google Workspace MCP server end-to-end tests.

Boots the Google Workspace twin via uvicorn, verifies the tool surface, and
exercises Gmail + Drive tools — confirming mutations are visible via REST /_state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

GOOGLE_WORKSPACE_TOOL_NAMES = {
    # Gmail
    "gmail_get_profile",
    "gmail_list_labels",
    "gmail_get_label",
    "gmail_create_label",
    "gmail_update_label",
    "gmail_delete_label",
    "gmail_list_threads",
    "gmail_get_thread",
    "gmail_modify_thread",
    "gmail_trash_thread",
    "gmail_delete_thread",
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_send_message",
    "gmail_modify_message",
    "gmail_trash_message",
    "gmail_delete_message",
    "gmail_list_drafts",
    "gmail_get_draft",
    "gmail_create_draft",
    "gmail_update_draft",
    "gmail_send_draft",
    "gmail_delete_draft",
    # Drive
    "drive_list_files",
    "drive_get_file",
    "drive_create_file",
    "drive_create_folder",
    "drive_update_file",
    "drive_delete_file",
    "drive_copy_file",
    "drive_search_files",
    "drive_list_permissions",
    "drive_add_permission",
    "drive_update_permission",
    "drive_remove_permission",
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
def gw_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.google_workspace:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"GW twin failed to start on port {port}"
        httpx.post(f"http://127.0.0.1:{port}/_seed/small-team").raise_for_status()
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_gw_mcp_lists_expected_tools(gw_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{gw_twin}/mcp/"
    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert GOOGLE_WORKSPACE_TOOL_NAMES.issubset(names), (
                f"missing tools: {GOOGLE_WORKSPACE_TOOL_NAMES - names}"
            )


@pytest.mark.asyncio
async def test_gw_mcp_gmail_tools(gw_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{gw_twin}/mcp/"
    state_url = f"http://127.0.0.1:{gw_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. get_profile
            result = await session.call_tool("gmail_get_profile", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "emailAddress" in text

            # 2. list_labels
            result = await session.call_tool("gmail_list_labels", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "INBOX" in text

            # 3. create_label
            result = await session.call_tool("gmail_create_label", {"name": "MCP-Label"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "MCP-Label" in text

            state = httpx.get(state_url).json()
            assert any(
                lab["name"] == "MCP-Label"
                for lab in state["gmail_labels"].values()
            )

            # 4. send_message
            result = await session.call_tool("gmail_send_message", {
                "to": "bob@acme.test",
                "subject": "MCP test",
                "body": "Hello from MCP!",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "id" in text

            state = httpx.get(state_url).json()
            assert any(
                "SENT" in m.get("labelIds", [])
                for m in state["gmail_messages"].values()
            )

            # 5. list_threads
            result = await session.call_tool("gmail_list_threads", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "threads" in text or "resultSizeEstimate" in text

            # 6. create_draft
            result = await session.call_tool("gmail_create_draft", {
                "to": "carol@acme.test",
                "subject": "Draft Test",
                "body": "Draft body text",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "id" in text


@pytest.mark.asyncio
async def test_gw_mcp_drive_tools(gw_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{gw_twin}/mcp/"
    state_url = f"http://127.0.0.1:{gw_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_files
            result = await session.call_tool("drive_list_files", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "files" in text

            # 2. create_file
            result = await session.call_tool("drive_create_file", {
                "name": "mcp-report.txt",
                "mime_type": "text/plain",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp-report.txt" in text

            state = httpx.get(state_url).json()
            file_id = next(
                fid for fid, f in state["drive_files"].items()
                if f["name"] == "mcp-report.txt"
            )

            # 3. get_file
            result = await session.call_tool("drive_get_file", {"file_id": file_id})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp-report.txt" in text

            # 4. add_permission
            result = await session.call_tool("drive_add_permission", {
                "file_id": file_id,
                "role": "reader",
                "permission_type": "user",
                "email_address": "viewer@test.com",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "reader" in text

            state = httpx.get(state_url).json()
            assert state["drive_permissions"].get(file_id)

            # 5. search_files
            result = await session.call_tool("drive_search_files", {"query": "mcp-report"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "mcp-report.txt" in text

            # 6. create_folder
            result = await session.call_tool("drive_create_folder", {"name": "MCP-Folder"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "MCP-Folder" in text

            # 7. delete_file
            result = await session.call_tool("drive_delete_file", {"file_id": file_id})
            state = httpx.get(state_url).json()
            assert file_id not in state["drive_files"]
