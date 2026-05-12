"""Stripe twin: stateful in-memory clone of Stripe REST API.

Phase 3 plan 03 (strict mode + auth + idempotency).
Phase 3 plan 04 adds extended-mode endpoints behind STRIPE_STRICT=false +
rate-limit middleware + seeds.

Real Stripe accepts both application/x-www-form-urlencoded (canonical) and
application/json. We accept both and normalize to a dict.

Idempotency: when a POST request includes `Idempotency-Key: <k>`, we cache
(key, path, body-hash) -> (status, body). A retry with the same key & body
returns the cached response and skips mutation. Different body with same key
returns the cached response anyway (matches Stripe's behaviour — the key is
the contract).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="checkpoint stripe twin")

DEFAULT_BOOTSTRAP_TOKEN = "sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789"
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "stripe_seeds"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_unix() -> int:
    return int(time.time())


def _fresh_state() -> dict:
    return {
        "customers": {},          # cus_xxx -> dict
        "products": {},           # prod_xxx
        "prices": {},             # price_xxx
        "payment_intents": {},    # pi_xxx
        "refunds": {},            # re_xxx
        "invoices": {},           # in_xxx
        "invoice_items": {},      # ii_xxx
        "subscriptions": {},      # sub_xxx
        "coupons": {},            # cou_xxx (Stripe IDs)
        "payment_links": {},      # plink_xxx
        "disputes": {},           # du_xxx
        "balance": {
            "available": [{"amount": 0, "currency": "usd"}],
            "pending":   [{"amount": 0, "currency": "usd"}],
            "object": "balance",
        },
        "account": {
            "id": "acct_test_checkpoint",
            "object": "account",
            "business_profile": {"name": "Checkpoint Test Acct"},
            "country": "US",
            "default_currency": "usd",
            "email": "owner@acme.test",
            "type": "standard",
        },
        "_counters": {
            "customer": 0, "product": 0, "price": 0, "payment_intent": 0,
            "refund": 0, "invoice": 0, "invoice_item": 0, "subscription": 0,
            "coupon": 0, "payment_link": 0, "dispute": 0,
            "requests": 0,
        },
        "_idempotency": {},   # key -> {"path": ..., "body_hash": ..., "status": ..., "body": ...}
        "_config": {
            "rate_limit": None,
            "strict": True,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -------------------------------------------------------------

def stripe_error(status: int, message: str, *, type_: str = "invalid_request_error",
                 code: str | None = None, param: str | None = None) -> JSONResponse:
    err: dict[str, Any] = {"type": type_, "message": message}
    if code:
        err["code"] = code
    if param:
        err["param"] = param
    return JSONResponse(status_code=status, content={"error": err})


def _bootstrap_token() -> str:
    return os.environ.get("STRIPE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _strict_default() -> bool:
    val = os.environ.get("STRIPE_STRICT", "true").lower()
    return val not in ("false", "0", "no")


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    for prefix in ("Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return None


def _next_id(kind: str, prefix: str) -> str:
    STATE["_counters"][kind] += 1
    n = STATE["_counters"][kind]
    return f"{prefix}_{kind}{n:08d}"


def _hash_body(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:32]


async def _parse_body(request: Request) -> dict:
    """Parse JSON or x-www-form-urlencoded into a plain dict."""
    ct = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if not raw:
        return {}
    if "application/json" in ct:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    # Form-encoded (Stripe's canonical).
    try:
        from urllib.parse import parse_qs
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        out: dict[str, Any] = {}
        for k, vs in parsed.items():
            # Strip simple bracket suffixes like `metadata[foo]` -> nested dict
            if "[" in k and k.endswith("]"):
                top, sub = k.split("[", 1)
                sub = sub[:-1]
                out.setdefault(top, {})
                out[top][sub] = vs[0]
            else:
                out[k] = vs[0]
        return out
    except Exception:
        # Try JSON as fallback (some clients send JSON without Content-Type).
        try:
            return json.loads(raw)
        except Exception:
            return {}


# --- middlewares ---------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX):
        return await call_next(request)

    token = _extract_token(request.headers.get("authorization"))
    if not token:
        return stripe_error(
            401, "Did not provide API key.",
        )
    if token != _bootstrap_token():
        return stripe_error(
            401,
            "Invalid API Key provided: " + (token[:7] + "...") if len(token) > 10 else token,
            type_="invalid_request_error",
        )

    STATE["_counters"]["requests"] += 1
    return await call_next(request)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX):
        return await call_next(request)

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else None
    except Exception:
        body = body_bytes.decode("utf-8", errors="replace") if body_bytes else None

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}
    request._receive = receive  # type: ignore[attr-defined]

    response = await call_next(request)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    resp_bytes = b"".join(chunks)
    try:
        resp_body = json.loads(resp_bytes) if resp_bytes else None
    except Exception:
        resp_body = resp_bytes.decode("utf-8", errors="replace") if resp_bytes else None

    TRACE.append({
        "ts": _now_iso(),
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "body": body,
        "status": response.status_code,
        "response": resp_body,
    })
    return Response(
        content=resp_bytes,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        media_type=response.media_type,
    )


def _idempotency_serve(request: Request, body_bytes: bytes) -> JSONResponse | None:
    """If the request has an Idempotency-Key we've seen, replay the cached response."""
    key = request.headers.get("idempotency-key")
    if not key:
        return None
    cache = STATE["_idempotency"]
    cached = cache.get(key)
    if cached is None:
        return None
    return JSONResponse(status_code=cached["status"], content=cached["body"])


def _idempotency_store(request: Request, status: int, body: dict) -> None:
    key = request.headers.get("idempotency-key")
    if not key:
        return
    STATE["_idempotency"][key] = {
        "path": request.url.path,
        "status": status,
        "body": body,
    }


# --- introspection -------------------------------------------------------

@app.get("/_health")
def health():
    return {"ok": True}


@app.get("/_trace")
def get_trace():
    return TRACE


@app.get("/_state")
def get_state():
    return {k: v for k, v in STATE.items() if not k.startswith("_") or k == "_config"}


@app.post("/_reset")
def reset():
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    return {"ok": True}


@app.post("/_config")
async def set_config(request: Request):
    body = await request.json()
    cfg = STATE["_config"]
    if "rate_limit" in body:
        cfg["rate_limit"] = body["rate_limit"]
    if "strict" in body:
        cfg["strict"] = bool(body["strict"])
    return {"ok": True, "config": cfg}


@app.post("/_seed/{name}")
def load_seed(name: str):
    path = SEEDS_DIR / f"{name}.json"
    if not path.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"seed {name!r} not found"})
    data = json.loads(path.read_text())
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    for k, v in (data.get("state") or {}).items():
        if isinstance(v, dict) and isinstance(STATE.get(k), dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    cfg = data.get("config") or {}
    for ck, cv in cfg.items():
        STATE["_config"][ck] = cv
    return {"ok": True, "seed": name, "config": STATE["_config"]}


# --- customers ----------------------------------------------------------

def _strict() -> bool:
    return bool(STATE["_config"].get("strict", True))


@app.post("/v1/customers")
async def create_customer(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    cid = _next_id("customer", "cus")
    cust = {
        "id": cid,
        "object": "customer",
        "email": body.get("email"),
        "name": body.get("name"),
        "description": body.get("description"),
        "phone": body.get("phone"),
        "created": _now_unix(),
        "balance": 0,
        "currency": body.get("currency", "usd"),
        "metadata": body.get("metadata") or {},
    }
    STATE["customers"][cid] = cust
    _idempotency_store(request, 200, cust)
    return cust


@app.get("/v1/customers")
def list_customers(limit: int = 10, starting_after: str | None = None):
    items = list(STATE["customers"].values())
    items.sort(key=lambda c: c["created"])
    if starting_after:
        items = [c for c in items if c["id"] > starting_after]
    page = items[:limit]
    return {
        "object": "list",
        "url": "/v1/customers",
        "has_more": len(items) > limit,
        "data": page,
    }


# --- products / prices --------------------------------------------------

@app.post("/v1/products")
async def create_product(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    name = body.get("name")
    if not name:
        return stripe_error(400, "Missing required param: name.", code="parameter_missing", param="name")
    pid = _next_id("product", "prod")
    prod = {
        "id": pid,
        "object": "product",
        "name": name,
        "description": body.get("description"),
        "active": True,
        "created": _now_unix(),
        "metadata": body.get("metadata") or {},
    }
    STATE["products"][pid] = prod
    _idempotency_store(request, 200, prod)
    return prod


@app.get("/v1/products")
def list_products(limit: int = 10):
    items = list(STATE["products"].values())
    return {"object": "list", "url": "/v1/products", "has_more": False, "data": items[:limit]}


@app.post("/v1/prices")
async def create_price(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    product = body.get("product")
    if not product:
        return stripe_error(400, "Missing required param: product.", code="parameter_missing", param="product")
    amount = body.get("unit_amount")
    if amount is None:
        return stripe_error(400, "Missing required param: unit_amount.", code="parameter_missing", param="unit_amount")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return stripe_error(400, "unit_amount must be an integer.", code="parameter_invalid_integer", param="unit_amount")
    pid = _next_id("price", "price")
    price = {
        "id": pid,
        "object": "price",
        "product": product,
        "unit_amount": amount,
        "currency": body.get("currency", "usd"),
        "active": True,
        "created": _now_unix(),
        "type": "recurring" if body.get("recurring") else "one_time",
        "recurring": body.get("recurring"),
    }
    STATE["prices"][pid] = price
    _idempotency_store(request, 200, price)
    return price


@app.get("/v1/prices")
def list_prices(limit: int = 10, product: str | None = None):
    items = list(STATE["prices"].values())
    if product:
        items = [p for p in items if p["product"] == product]
    return {"object": "list", "url": "/v1/prices", "has_more": False, "data": items[:limit]}


# --- payment_intents (strict: list only) --------------------------------

@app.get("/v1/payment_intents")
def list_payment_intents(limit: int = 10, customer: str | None = None):
    items = list(STATE["payment_intents"].values())
    if customer:
        items = [p for p in items if p.get("customer") == customer]
    items.sort(key=lambda p: p.get("created", 0), reverse=True)
    return {"object": "list", "url": "/v1/payment_intents", "has_more": False, "data": items[:limit]}


# --- refunds ------------------------------------------------------------

@app.post("/v1/refunds")
async def create_refund(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    pi = body.get("payment_intent")
    charge = body.get("charge")
    if not pi and not charge:
        return stripe_error(400, "One of payment_intent or charge is required.",
                            code="parameter_missing", param="payment_intent")
    rid = _next_id("refund", "re")
    amount = body.get("amount")
    try:
        amount = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        return stripe_error(400, "amount must be an integer.", code="parameter_invalid_integer", param="amount")
    if pi and pi in STATE["payment_intents"]:
        intent = STATE["payment_intents"][pi]
        if amount is None:
            amount = intent.get("amount")
        intent["status"] = "refunded" if amount == intent.get("amount") else "partially_refunded"
    refund = {
        "id": rid,
        "object": "refund",
        "amount": amount or 0,
        "currency": body.get("currency", "usd"),
        "payment_intent": pi,
        "charge": charge,
        "reason": body.get("reason"),
        "status": "succeeded",
        "created": _now_unix(),
    }
    STATE["refunds"][rid] = refund
    _idempotency_store(request, 200, refund)
    return refund


@app.get("/v1/refunds")
def list_refunds(limit: int = 10, payment_intent: str | None = None):
    items = list(STATE["refunds"].values())
    if payment_intent:
        items = [r for r in items if r.get("payment_intent") == payment_intent]
    return {"object": "list", "url": "/v1/refunds", "has_more": False, "data": items[:limit]}


# --- invoices + invoice items ------------------------------------------

@app.post("/v1/invoices")
async def create_invoice(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    customer = body.get("customer")
    if not customer:
        return stripe_error(400, "Missing required param: customer.", code="parameter_missing", param="customer")
    iid = _next_id("invoice", "in")
    invoice = {
        "id": iid,
        "object": "invoice",
        "customer": customer,
        "status": "draft",
        "amount_due": 0,
        "amount_paid": 0,
        "currency": body.get("currency", "usd"),
        "description": body.get("description"),
        "lines": {"object": "list", "data": [], "has_more": False},
        "created": _now_unix(),
    }
    STATE["invoices"][iid] = invoice
    _idempotency_store(request, 200, invoice)
    return invoice


@app.get("/v1/invoices")
def list_invoices(limit: int = 10, customer: str | None = None, status: str | None = None):
    items = list(STATE["invoices"].values())
    if customer:
        items = [i for i in items if i["customer"] == customer]
    if status:
        items = [i for i in items if i["status"] == status]
    return {"object": "list", "url": "/v1/invoices", "has_more": False, "data": items[:limit]}


@app.post("/v1/invoiceitems")
async def create_invoice_item(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    customer = body.get("customer")
    if not customer:
        return stripe_error(400, "Missing required param: customer.", code="parameter_missing", param="customer")
    iiid = _next_id("invoice_item", "ii")
    amount = body.get("amount") or body.get("unit_amount") or 0
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    item = {
        "id": iiid,
        "object": "invoiceitem",
        "customer": customer,
        "amount": amount,
        "currency": body.get("currency", "usd"),
        "description": body.get("description"),
        "invoice": body.get("invoice"),
        "created": _now_unix(),
    }
    STATE["invoice_items"][iiid] = item
    # Attach to invoice if specified.
    inv_id = body.get("invoice")
    if inv_id and inv_id in STATE["invoices"]:
        inv = STATE["invoices"][inv_id]
        inv["lines"]["data"].append(item)
        inv["amount_due"] += amount
    _idempotency_store(request, 200, item)
    return item


@app.post("/v1/invoices/{invoice_id}/finalize")
async def finalize_invoice(invoice_id: str, request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    inv = STATE["invoices"].get(invoice_id)
    if inv is None:
        return stripe_error(404, "No such invoice: " + invoice_id, code="resource_missing")
    inv["status"] = "open"
    _idempotency_store(request, 200, inv)
    return inv


# --- subscriptions ------------------------------------------------------

@app.get("/v1/subscriptions")
def list_subscriptions(limit: int = 10, customer: str | None = None, status: str | None = None):
    items = list(STATE["subscriptions"].values())
    if customer:
        items = [s for s in items if s["customer"] == customer]
    if status:
        items = [s for s in items if s["status"] == status]
    return {"object": "list", "url": "/v1/subscriptions", "has_more": False, "data": items[:limit]}


@app.post("/v1/subscriptions/{sub_id}")
async def update_subscription(sub_id: str, request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    sub = STATE["subscriptions"].get(sub_id)
    if sub is None:
        return stripe_error(404, "No such subscription: " + sub_id, code="resource_missing")
    body = await _parse_body(request)
    if "cancel_at_period_end" in body:
        sub["cancel_at_period_end"] = body["cancel_at_period_end"] in (True, "true", "1", 1)
    if "metadata" in body and isinstance(body["metadata"], dict):
        sub.setdefault("metadata", {}).update(body["metadata"])
    if body.get("status") in ("active", "past_due", "canceled", "trialing", "paused"):
        sub["status"] = body["status"]
    _idempotency_store(request, 200, sub)
    return sub


@app.delete("/v1/subscriptions/{sub_id}")
def cancel_subscription(sub_id: str):
    sub = STATE["subscriptions"].get(sub_id)
    if sub is None:
        return stripe_error(404, "No such subscription: " + sub_id, code="resource_missing")
    sub["status"] = "canceled"
    sub["canceled_at"] = _now_unix()
    return sub


# --- balance ------------------------------------------------------------

@app.get("/v1/balance")
def retrieve_balance():
    return STATE["balance"]


# --- coupons ------------------------------------------------------------

@app.post("/v1/coupons")
async def create_coupon(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    cid = body.get("id") or _next_id("coupon", "cou")
    coupon = {
        "id": cid,
        "object": "coupon",
        "name": body.get("name"),
        "percent_off": body.get("percent_off"),
        "amount_off": body.get("amount_off"),
        "currency": body.get("currency"),
        "duration": body.get("duration", "once"),
        "valid": True,
        "created": _now_unix(),
    }
    STATE["coupons"][cid] = coupon
    _idempotency_store(request, 200, coupon)
    return coupon


@app.get("/v1/coupons")
def list_coupons(limit: int = 10):
    items = list(STATE["coupons"].values())
    return {"object": "list", "url": "/v1/coupons", "has_more": False, "data": items[:limit]}


# --- payment_links ------------------------------------------------------

@app.post("/v1/payment_links")
async def create_payment_link(request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    body = await _parse_body(request)
    line_items = body.get("line_items")
    if not line_items:
        return stripe_error(400, "Missing required param: line_items.", code="parameter_missing", param="line_items")
    pid = _next_id("payment_link", "plink")
    link = {
        "id": pid,
        "object": "payment_link",
        "active": True,
        "url": f"https://checkout.stripe-test.local/c/pay/{pid}",
        "line_items": line_items if isinstance(line_items, list) else [line_items],
        "created": _now_unix(),
    }
    STATE["payment_links"][pid] = link
    _idempotency_store(request, 200, link)
    return link


# --- disputes -----------------------------------------------------------

@app.get("/v1/disputes")
def list_disputes(limit: int = 10):
    items = list(STATE["disputes"].values())
    return {"object": "list", "url": "/v1/disputes", "has_more": False, "data": items[:limit]}


@app.post("/v1/disputes/{dispute_id}")
async def update_dispute(dispute_id: str, request: Request):
    cached = _idempotency_serve(request, await request.body())
    if cached is not None:
        return cached
    dispute = STATE["disputes"].get(dispute_id)
    if dispute is None:
        return stripe_error(404, "No such dispute: " + dispute_id, code="resource_missing")
    body = await _parse_body(request)
    if "evidence" in body and isinstance(body["evidence"], dict):
        dispute.setdefault("evidence", {}).update(body["evidence"])
    if "submit" in body:
        dispute["status"] = "under_review"
    _idempotency_store(request, 200, dispute)
    return dispute


# --- search / fetch / account ------------------------------------------

@app.get("/v1/search")
def search_resources(query: str = "", limit: int = 10):
    """Loose substring search across customers + products + invoices + subscriptions."""
    q = query.lower()
    hits: list[dict] = []
    for coll, kind in (
        ("customers", "customer"), ("products", "product"),
        ("invoices", "invoice"), ("subscriptions", "subscription"),
    ):
        for obj in STATE[coll].values():
            blob = json.dumps(obj).lower()
            if not q or q in blob:
                hits.append({**obj, "_kind": kind})
    return {"object": "search_result", "url": "/v1/search", "has_more": False, "data": hits[:limit]}


@app.get("/v1/files")
def fetch_resources(limit: int = 10):
    """Placeholder for fetch_stripe_resources — empty list. Stripe MCP stub."""
    return {"object": "list", "url": "/v1/files", "has_more": False, "data": []}


@app.get("/v1/account")
def get_account_info():
    return STATE["account"]
