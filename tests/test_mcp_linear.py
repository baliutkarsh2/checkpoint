"""Linear MCP server end-to-end tests.

Boots the Linear twin via uvicorn, verifies the tool surface, and exercises
key tools — confirming mutations are visible via REST /_state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

LINEAR_TOOL_NAMES = {
    "linear_get_organization",
    "linear_list_teams",
    "linear_get_team",
    "linear_create_team",
    "linear_list_workflow_states",
    "linear_list_issues",
    "linear_get_issue",
    "linear_create_issue",
    "linear_update_issue",
    "linear_archive_issue",
    "linear_search_issues",
    "linear_create_comment",
    "linear_list_comments",
    "linear_list_projects",
    "linear_get_project",
    "linear_create_project",
    "linear_update_project",
    "linear_list_labels",
    "linear_create_label",
    "linear_list_cycles",
    "linear_create_cycle",
    "linear_list_users",
    "linear_get_user",
    "linear_get_viewer",
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
def linear_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.linear:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"Linear twin failed to start on port {port}"
        httpx.post(f"http://127.0.0.1:{port}/_seed/small-project").raise_for_status()
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_linear_mcp_lists_expected_tools(linear_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{linear_twin}/mcp/"
    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert LINEAR_TOOL_NAMES.issubset(names), (
                f"missing tools: {LINEAR_TOOL_NAMES - names}"
            )


@pytest.mark.asyncio
async def test_linear_mcp_key_tools(linear_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{linear_twin}/mcp/"
    state_url = f"http://127.0.0.1:{linear_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_teams
            result = await session.call_tool("linear_list_teams", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "Engineering" in text or "team" in text.lower()

            # 2. list_issues — seed has issues
            result = await session.call_tool("linear_list_issues", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "nodes" in text

            # 3. create_issue
            result = await session.call_tool("linear_create_issue", {
                "title": "MCP test issue",
                "teamId": "team-engineering",
            })
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "MCP test issue" in text

            # Verify state mutation
            state = httpx.get(state_url).json()
            assert any(
                i["title"] == "MCP test issue"
                for i in state["issues"].values()
            )

            # 4. list_projects
            result = await session.call_tool("linear_list_projects", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "nodes" in text

            # 5. get_organization
            result = await session.call_tool("linear_get_organization", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "id" in text
