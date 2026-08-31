"""Stripe MCP server — wraps `checkpoint.twins.stripe` REST surface.

Tool names match Archal's strict-mode tool list (SCOPE.md §3.5): 26
tools (24 live REST shims + 2 documented-as-stub tools). Each tool body
is a thin REST shim — REST and MCP share one `STATE` dict.

The two Archal-documented stubs are `search_stripe_documentation` and
`stripe_integration_recommender`. We additionally ship
`send_stripe_mcp_feedback` as a no-op (Archal lists it in §3.5 strict
mode and it's documented as a stub). All three return inert success
envelopes so MCP hosts that auto-introspect get the full Archal
surface, but no twin state changes.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from ._shim import make_shim, mount_mcp_on_fastapi
from checkpoint.fake_credentials import FAKE_STRIPE_KEY


STRIPE_BOOTSTRAP_TOKEN = FAKE_STRIPE_KEY


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Stripe twin."""
    token = os.environ.get("STRIPE_BOOTSTRAP_TOKEN", STRIPE_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = FastMCP(
        name="checkpoint-stripe",
        instructions="Stateful synthetic Stripe (strict mode). Tool names match Archal §3.5.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ----- Customers ----------------------------------------------------

    @mcp.tool()
    async def create_customer(
        email: str | None = None,
        name: str | None = None,
        description: str | None = None,
        phone: str | None = None,
    ) -> Any:
        """Create a customer."""
        body: dict[str, Any] = {}
        if email is not None:
            body["email"] = email
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if phone is not None:
            body["phone"] = phone
        return await shim("POST", "/v1/customers", json=body)

    @mcp.tool()
    async def list_customers(
        limit: int = 10, starting_after: str | None = None
    ) -> Any:
        """Paginated list of customers."""
        params: dict[str, Any] = {"limit": limit}
        if starting_after is not None:
            params["starting_after"] = starting_after
        return await shim("GET", "/v1/customers", params=params)

    # ----- Products / Prices --------------------------------------------

    @mcp.tool()
    async def create_product(
        name: str, description: str | None = None
    ) -> Any:
        """Create a product."""
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        return await shim("POST", "/v1/products", json=body)

    @mcp.tool()
    async def list_products(limit: int = 10) -> Any:
        """List products."""
        return await shim("GET", "/v1/products", params={"limit": limit})

    @mcp.tool()
    async def create_price(
        product: str,
        unit_amount: int,
        currency: str = "usd",
        recurring: dict[str, Any] | None = None,
    ) -> Any:
        """Create a price for a product."""
        body: dict[str, Any] = {
            "product": product, "unit_amount": unit_amount, "currency": currency,
        }
        if recurring is not None:
            body["recurring"] = recurring
        return await shim("POST", "/v1/prices", json=body)

    @mcp.tool()
    async def list_prices(limit: int = 10, product: str | None = None) -> Any:
        """List prices, optionally filtered by product."""
        params: dict[str, Any] = {"limit": limit}
        if product is not None:
            params["product"] = product
        return await shim("GET", "/v1/prices", params=params)

    # ----- Payment intents (strict: list only) -------------------------

    @mcp.tool()
    async def list_payment_intents(
        limit: int = 10, customer: str | None = None
    ) -> Any:
        """List payment intents, newest first."""
        params: dict[str, Any] = {"limit": limit}
        if customer is not None:
            params["customer"] = customer
        return await shim("GET", "/v1/payment_intents", params=params)

    # ----- Refunds ------------------------------------------------------

    @mcp.tool()
    async def create_refund(
        payment_intent: str | None = None,
        charge: str | None = None,
        amount: int | None = None,
        reason: str | None = None,
    ) -> Any:
        """Create a refund against a payment intent or charge."""
        body: dict[str, Any] = {}
        if payment_intent is not None:
            body["payment_intent"] = payment_intent
        if charge is not None:
            body["charge"] = charge
        if amount is not None:
            body["amount"] = amount
        if reason is not None:
            body["reason"] = reason
        return await shim("POST", "/v1/refunds", json=body)

    @mcp.tool()
    async def list_refunds(
        limit: int = 10, payment_intent: str | None = None
    ) -> Any:
        """List refunds, optionally filtered by payment_intent."""
        params: dict[str, Any] = {"limit": limit}
        if payment_intent is not None:
            params["payment_intent"] = payment_intent
        return await shim("GET", "/v1/refunds", params=params)

    # ----- Invoices + invoice items ------------------------------------

    @mcp.tool()
    async def create_invoice(
        customer: str,
        description: str | None = None,
        currency: str = "usd",
    ) -> Any:
        """Create a draft invoice for a customer."""
        body: dict[str, Any] = {"customer": customer, "currency": currency}
        if description is not None:
            body["description"] = description
        return await shim("POST", "/v1/invoices", json=body)

    @mcp.tool()
    async def list_invoices(
        limit: int = 10, customer: str | None = None
    ) -> Any:
        """List invoices, optionally filtered by customer."""
        params: dict[str, Any] = {"limit": limit}
        if customer is not None:
            params["customer"] = customer
        return await shim("GET", "/v1/invoices", params=params)

    @mcp.tool()
    async def create_invoice_item(
        customer: str,
        amount: int,
        currency: str = "usd",
        description: str | None = None,
        invoice: str | None = None,
    ) -> Any:
        """Create a one-off line item on a customer or specific invoice."""
        body: dict[str, Any] = {
            "customer": customer, "amount": amount, "currency": currency,
        }
        if description is not None:
            body["description"] = description
        if invoice is not None:
            body["invoice"] = invoice
        return await shim("POST", "/v1/invoiceitems", json=body)

    @mcp.tool()
    async def finalize_invoice(invoice: str) -> Any:
        """Finalize a draft invoice (status draft -> open)."""
        return await shim("POST", f"/v1/invoices/{invoice}/finalize")

    # ----- Subscriptions ------------------------------------------------

    @mcp.tool()
    async def list_subscriptions(
        limit: int = 10,
        customer: str | None = None,
        status: str | None = None,
    ) -> Any:
        """List subscriptions filtered by customer and/or status."""
        params: dict[str, Any] = {"limit": limit}
        if customer is not None:
            params["customer"] = customer
        if status is not None:
            params["status"] = status
        return await shim("GET", "/v1/subscriptions", params=params)

    @mcp.tool()
    async def update_subscription(
        subscription: str,
        cancel_at_period_end: bool | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Update a subscription."""
        body: dict[str, Any] = {}
        if cancel_at_period_end is not None:
            body["cancel_at_period_end"] = cancel_at_period_end
        if status is not None:
            body["status"] = status
        if metadata is not None:
            body["metadata"] = metadata
        return await shim(
            "POST", f"/v1/subscriptions/{subscription}", json=body,
        )

    @mcp.tool()
    async def cancel_subscription(subscription: str) -> Any:
        """Cancel a subscription immediately."""
        return await shim("DELETE", f"/v1/subscriptions/{subscription}")

    # ----- Balance ------------------------------------------------------

    @mcp.tool()
    async def retrieve_balance() -> Any:
        """Retrieve the account balance."""
        return await shim("GET", "/v1/balance")

    # ----- Coupons ------------------------------------------------------

    @mcp.tool()
    async def create_coupon(
        id: str | None = None,
        name: str | None = None,
        percent_off: float | None = None,
        amount_off: int | None = None,
        currency: str | None = None,
        duration: str = "once",
    ) -> Any:
        """Create a coupon."""
        body: dict[str, Any] = {"duration": duration}
        if id is not None:
            body["id"] = id
        if name is not None:
            body["name"] = name
        if percent_off is not None:
            body["percent_off"] = percent_off
        if amount_off is not None:
            body["amount_off"] = amount_off
        if currency is not None:
            body["currency"] = currency
        return await shim("POST", "/v1/coupons", json=body)

    @mcp.tool()
    async def list_coupons(limit: int = 10) -> Any:
        """List coupons."""
        return await shim("GET", "/v1/coupons", params={"limit": limit})

    # ----- Payment links ------------------------------------------------

    @mcp.tool()
    async def create_payment_link(line_items: list[dict[str, Any]]) -> Any:
        """Create a payment link for one or more line items."""
        return await shim(
            "POST", "/v1/payment_links", json={"line_items": line_items},
        )

    # ----- Disputes -----------------------------------------------------

    @mcp.tool()
    async def list_disputes(limit: int = 10) -> Any:
        """List disputes."""
        return await shim("GET", "/v1/disputes", params={"limit": limit})

    @mcp.tool()
    async def update_dispute(
        dispute: str,
        evidence: dict[str, Any] | None = None,
        submit: bool | None = None,
    ) -> Any:
        """Submit evidence on a dispute, or mark it submitted."""
        body: dict[str, Any] = {}
        if evidence is not None:
            body["evidence"] = evidence
        if submit is not None:
            body["submit"] = submit
        return await shim("POST", f"/v1/disputes/{dispute}", json=body)

    # ----- Search / fetch / account info -------------------------------

    @mcp.tool()
    async def search_stripe_resources(query: str, limit: int = 10) -> Any:
        """Loose substring search across customers/products/invoices/subscriptions."""
        return await shim(
            "GET", "/v1/search", params={"query": query, "limit": limit},
        )

    @mcp.tool()
    async def fetch_stripe_resources(limit: int = 10) -> Any:
        """Fetch arbitrary Stripe resources (Archal docs this as stub-ish)."""
        return await shim("GET", "/v1/files", params={"limit": limit})

    @mcp.tool()
    async def get_stripe_account_info() -> Any:
        """Get the account-level info object."""
        return await shim("GET", "/v1/account")

    # ----- Documented stubs (no twin call) -----------------------------

    @mcp.tool()
    async def search_stripe_documentation(query: str) -> Any:
        """Documented as a stub in Archal §3.5. Returns an empty result list."""
        return {"data": [], "_stub": True, "query": query}

    @mcp.tool()
    async def stripe_integration_recommender(use_case: str | None = None) -> Any:
        """Documented as a stub in Archal §3.5. Returns an empty list."""
        return {"recommendations": [], "_stub": True, "use_case": use_case}

    @mcp.tool()
    async def send_stripe_mcp_feedback(
        feedback: str, category: str | None = None
    ) -> Any:
        """Documented as a stub in Archal §3.5. Always returns ok."""
        return {"ok": True, "_stub": True, "category": category}

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Stripe FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
