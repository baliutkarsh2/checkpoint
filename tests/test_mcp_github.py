"""Phase 6 Plan 01: GitHub MCP server end-to-end through the official client.

Spawns the GitHub twin with uvicorn on a free port, opens an MCP
ClientSession over streamable HTTP at `/mcp`, lists tools, calls 5
representative mutating tools, and verifies that `/_state` reflects
each mutation — proving the MCP transport and the REST transport share
one state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN

GITHUB_TOOL_NAMES = {
    # Repositories (6)
    "create_repository", "get_repository", "search_repositories",
    "fork_repository", "search_code", "search_users",
    # Files (3)
    "get_file_contents", "create_or_update_file", "push_files",
    # Branches (3)
    "create_branch", "list_branches", "delete_branch",
    # Issues (6)
    "create_issue", "get_issue", "list_issues", "update_issue",
    "search_issues", "add_issue_comment",
    # Pull requests (13)
    "create_pull_request", "get_pull_request", "list_pull_requests",
    "update_pull_request", "merge_pull_request", "get_pull_request_diff",
    "get_pull_request_commits", "get_pull_request_reviews",
    "create_pull_request_review", "get_pull_request_files",
    "get_pull_request_status", "update_pull_request_branch",
    "get_pull_request_comments",
    # Commits & workflows (3)
    "list_commits", "list_workflow_runs", "get_workflow_run",
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
def github_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.github:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), f"twin failed to start on port {port}"
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_github_mcp_lists_archal_tool_set(github_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{github_twin}/mcp/"
    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            missing = GITHUB_TOOL_NAMES - names
            extra = names - GITHUB_TOOL_NAMES
            assert not missing, f"missing tools: {missing}"
            assert not extra, f"unexpected tools: {extra}"


@pytest.mark.asyncio
async def test_github_mcp_mutations_share_state_with_rest(github_twin):
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    url = f"http://127.0.0.1:{github_twin}/mcp/"
    state_url = f"http://127.0.0.1:{github_twin}/_state"

    async with client_streams(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. create_repository
            await session.call_tool(
                "create_repository", {"name": "demo"},
            )
            state = httpx.get(state_url).json()
            assert "default-user/demo" in state["repos"], (
                "create_repository tool didn't mutate STATE.repos"
            )

            # 2. create_issue
            await session.call_tool(
                "create_issue",
                {"owner": "default-user", "repo": "demo", "title": "Bug"},
            )
            state = httpx.get(state_url).json()
            assert "default-user/demo#1" in state["issues"]
            assert state["issues"]["default-user/demo#1"]["title"] == "Bug"

            # 3. add_issue_comment
            await session.call_tool(
                "add_issue_comment",
                {
                    "owner": "default-user",
                    "repo": "demo",
                    "number": 1,
                    "body": "first comment",
                },
            )
            state = httpx.get(state_url).json()
            assert len(state["comments"]) == 1
            assert state["issues"]["default-user/demo#1"]["comments"] == 1

            # 4. create_branch
            await session.call_tool(
                "create_branch",
                {
                    "owner": "default-user",
                    "repo": "demo",
                    "branch": "feature",
                    "from_branch": "main",
                },
            )
            state = httpx.get(state_url).json()
            assert "feature" in state["repos"]["default-user/demo"]["branches"]

            # 5. list_issues (read-only, sanity check)
            result = await session.call_tool(
                "list_issues",
                {"owner": "default-user", "repo": "demo"},
            )
            # MCP CallToolResult.content is a list of content blocks; the
            # tool returned a list of dicts that fastmcp will serialize.
            # Easiest assertion: structuredContent or content text contains "Bug".
            payload_text = "".join(
                getattr(c, "text", "") for c in result.content
            )
            assert "Bug" in payload_text


@pytest.mark.asyncio
async def test_github_mcp_and_rest_see_same_state(github_twin):
    """Cross-transport: MCP creates a repo, REST creates a second; both visible
    in /_state via either transport."""
    from mcp import ClientSession

    from checkpoint.mcp_compat import client_streams

    mcp_url = f"http://127.0.0.1:{github_twin}/mcp/"
    state_url = f"http://127.0.0.1:{github_twin}/_state"
    headers = {
        "Authorization": f"token {FAKE_GITHUB_TOKEN}",
    }

    async with client_streams(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # MCP creates one repo.
            await session.call_tool(
                "create_repository", {"name": "via-mcp"},
            )

    # REST creates a second.
    httpx.post(
        f"http://127.0.0.1:{github_twin}/user/repos",
        json={"name": "via-rest"},
        headers=headers,
    ).raise_for_status()

    state = httpx.get(state_url).json()
    assert "default-user/via-mcp" in state["repos"]
    assert "default-user/via-rest" in state["repos"]
