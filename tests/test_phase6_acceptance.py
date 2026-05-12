"""Phase 6 acceptance: one twin per clone, two transports per twin.

Spins up all three twins (github, slack, stripe) on free ports using
the same uvicorn path the runner uses. For each twin:
1. Open an MCP ClientSession and call one mutating tool.
2. GET /_state on the same port and assert the mutation is reflected.
3. Call the equivalent REST endpoint directly (with bootstrap token)
   and assert that a *second* resource appears alongside the MCP-created
   one — proving REST and MCP share one STATE dict.

This is the literal MCP-01 + Phase 6 success criterion check:
"one twin, two doors".
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest


GITHUB_TOKEN = "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"
SLACK_TOKEN = "xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
STRIPE_TOKEN = "sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789"


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


def _start_twin(clone: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            f"checkpoint.twins.{clone}:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def three_twins():
    ports = {clone: _free_port() for clone in ("github", "slack", "stripe")}
    procs = {clone: _start_twin(clone, ports[clone]) for clone in ports}
    try:
        for clone, port in ports.items():
            assert _wait_healthy(port), f"{clone} failed to start on port {port}"
        yield ports
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_twin_mcp_url_helper():
    """The runner helper returns the canonical URL pattern."""
    from checkpoint.runner import twin_mcp_url

    assert twin_mcp_url(9000) == "http://127.0.0.1:9000/mcp/"
    assert twin_mcp_url("8080", host="0.0.0.0") == "http://0.0.0.0:8080/mcp/"


@pytest.mark.asyncio
async def test_three_twins_mcp_one_transport_two_doors(three_twins):
    """Phase 6 acceptance: each twin mutates via MCP, then REST sees it too.

    For each twin:
      1. MCP creates one resource.
      2. /_state reflects the MCP-created resource.
      3. REST creates a *second* resource using the bootstrap token.
      4. /_state shows both resources — proving the two transports
         share a single STATE dict (one twin, two doors).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from checkpoint.runner import twin_mcp_url

    gh_port, sl_port, st_port = (
        three_twins["github"], three_twins["slack"], three_twins["stripe"],
    )

    # --- GitHub ---------------------------------------------------------
    async with streamablehttp_client(twin_mcp_url(gh_port)) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool(
                "create_repository", {"name": "phase6-via-mcp"},
            )
    httpx.post(
        f"http://127.0.0.1:{gh_port}/user/repos",
        json={"name": "phase6-via-rest"},
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
    ).raise_for_status()
    gh_state = httpx.get(f"http://127.0.0.1:{gh_port}/_state").json()
    assert "default-user/phase6-via-mcp" in gh_state["repos"]
    assert "default-user/phase6-via-rest" in gh_state["repos"]

    # --- Slack ----------------------------------------------------------
    # Slack twin starts with empty state; seed a channel via /_seed-file
    # so post_message has somewhere to land.
    httpx.post(
        f"http://127.0.0.1:{sl_port}/_seed-file",
        json={"state": {"channels": {"C_GENERAL": {
            "id": "C_GENERAL", "name": "general", "is_channel": True,
        }}}},
    ).raise_for_status()

    async with streamablehttp_client(twin_mcp_url(sl_port)) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool(
                "slack_post_message",
                {"channel": "C_GENERAL", "text": "hi from MCP"},
            )
    httpx.post(
        f"http://127.0.0.1:{sl_port}/api/chat.postMessage",
        json={"channel": "C_GENERAL", "text": "hi from REST"},
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
    ).raise_for_status()
    sl_state = httpx.get(f"http://127.0.0.1:{sl_port}/_state").json()
    texts = [m["text"] for m in sl_state["messages"].get("C_GENERAL", [])]
    assert "hi from MCP" in texts
    assert "hi from REST" in texts

    # --- Stripe ---------------------------------------------------------
    async with streamablehttp_client(twin_mcp_url(st_port)) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool(
                "create_customer",
                {"email": "via-mcp@example.com"},
            )
    httpx.post(
        f"http://127.0.0.1:{st_port}/v1/customers",
        json={"email": "via-rest@example.com"},
        headers={"Authorization": f"Bearer {STRIPE_TOKEN}"},
    ).raise_for_status()
    st_state = httpx.get(f"http://127.0.0.1:{st_port}/_state").json()
    emails = [c["email"] for c in st_state["customers"].values()]
    assert "via-mcp@example.com" in emails
    assert "via-rest@example.com" in emails
