"""Phase 6 Plan 02: Stripe MCP server end-to-end through the official client.

Boots the Stripe twin via uvicorn, opens an MCP client at /mcp, verifies
the 28-tool surface from SCOPE §3.5 strict-mode list, and calls a
representative subset against the empty seed — confirming each
mutation is visible in /_state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

STRIPE_TOOL_NAMES = {
    # Customers
    "create_customer", "list_customers",
    # Products / Prices
    "create_product", "list_products", "create_price", "list_prices",
    # Payment intents (strict: list only)
    "list_payment_intents",
    # Refunds
    "create_refund", "list_refunds",
    # Invoices + items
    "create_invoice", "list_invoices", "create_invoice_item",
    "finalize_invoice",
    # Subscriptions
    "list_subscriptions", "update_subscription", "cancel_subscription",
    # Balance
    "retrieve_balance",
    # Coupons
    "create_coupon", "list_coupons",
    # Payment links
    "create_payment_link",
    # Disputes
    "list_disputes", "update_dispute",
    # Search / fetch / account
    "search_stripe_resources", "fetch_stripe_resources",
    "search_stripe_documentation",
    "get_stripe_account_info",
    "stripe_integration_recommender",
    "send_stripe_mcp_feedback",
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
def stripe_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "checkpoint.twins.stripe:app",
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
async def test_stripe_mcp_lists_archal_tool_set(stripe_twin):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{stripe_twin}/mcp/"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == STRIPE_TOOL_NAMES, (
                f"missing: {STRIPE_TOOL_NAMES - names}, "
                f"extra: {names - STRIPE_TOOL_NAMES}"
            )


@pytest.mark.asyncio
async def test_stripe_mcp_representative_tools_callable(stripe_twin):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{stripe_twin}/mcp/"
    state_url = f"http://127.0.0.1:{stripe_twin}/_state"

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. create_customer
            await session.call_tool(
                "create_customer",
                {"email": "demo@example.com", "name": "Demo"},
            )
            state = httpx.get(state_url).json()
            assert state["customers"], "create_customer should mutate STATE.customers"
            customer_id = next(iter(state["customers"].keys()))

            # 2. list_customers (read-only sanity)
            listed = await session.call_tool("list_customers", {"limit": 5})
            listed_txt = "".join(getattr(c, "text", "") for c in listed.content)
            assert customer_id in listed_txt

            # 3. create_product
            await session.call_tool("create_product", {"name": "Plus Plan"})
            state = httpx.get(state_url).json()
            assert state["products"], "create_product should mutate STATE.products"

            # 4. create_refund (no real payment_intent — refund records anyway)
            await session.call_tool(
                "create_refund",
                {"payment_intent": "pi_does_not_exist", "amount": 500},
            )
            state = httpx.get(state_url).json()
            assert state["refunds"], "create_refund should mutate STATE.refunds"

            # 5. retrieve_balance (read-only)
            bal = await session.call_tool("retrieve_balance", {})
            bal_txt = "".join(getattr(c, "text", "") for c in bal.content)
            assert "balance" in bal_txt


@pytest.mark.asyncio
async def test_stripe_mcp_documented_stubs_return_inert_envelopes(stripe_twin):
    """The three Archal-documented stubs return ok-but-empty payloads."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{stripe_twin}/mcp/"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for stub_name, args in (
                ("search_stripe_documentation", {"query": "anything"}),
                ("stripe_integration_recommender", {"use_case": "fintech"}),
                ("send_stripe_mcp_feedback", {"feedback": "great"}),
            ):
                result = await session.call_tool(stub_name, args)
                text = "".join(getattr(c, "text", "") for c in result.content)
                assert "_stub" in text, f"stub {stub_name} missing _stub marker"
