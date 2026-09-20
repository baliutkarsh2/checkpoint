"""Stripe twin: a stateful, in-memory Stripe API.

Accepts both ``application/x-www-form-urlencoded`` (Stripe's canonical encoding,
including the SDKs' nested ``a[b][0]=v`` bracket paths) and JSON, and answers
with Stripe's own object, list, search and error envelopes, so the official SDKs
hydrate the responses into their typed objects instead of tripping over them.

What it models, roughly in the order agents reach for it: customers, products
and prices, payment intents with their charges and refunds, invoices and invoice
items, subscriptions, checkout sessions, payment methods, coupons, payment
links, disputes, events and the account balance. Every mutation writes through
to ``STATE`` and records an ``Event``, so a run can be asserted on afterwards.

Idempotency follows Stripe: a POST with ``Idempotency-Key: <k>`` caches its
response and a retry replays it; the same key with different parameters, or on a
different endpoint, is a 400 ``idempotency_error``.

The control plane and fault model come from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_STRIPE_KEY
from checkpoint.twins import kit

app = FastAPI(title="checkpoint stripe twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_STRIPE_KEY

SEEDS_DIR = Path(__file__).parent / "stripe_seeds"

# The API version stripe-python 15.x pins; echoed on events and responses.
API_VERSION = "2026-08-26.dahlia"

# Hosted-page URLs point at a sandbox host, never at Stripe's real domains.
CHECKOUT_HOST = "https://checkout.stripe-test.local"
DASHBOARD_HOST = "https://dashboard.stripe-test.local"

# Collections in STATE, and the Stripe ID prefix each one's counter mints.
_PREFIXES: dict[str, str] = {
    "customer": "cus", "product": "prod", "price": "price", "payment_method": "pm",
    "payment_intent": "pi", "charge": "ch", "refund": "re", "invoice": "in",
    "invoice_item": "ii", "line_item": "il", "subscription": "sub",
    "subscription_item": "si", "checkout_session": "cs_test", "coupon": "cou",
    "payment_link": "plink", "dispute": "du", "event": "evt",
    "balance_transaction": "txn",
}

# Collection name -> the counter kind that mints its IDs.
_COLLECTIONS: dict[str, str] = {
    "customers": "customer", "products": "product", "prices": "price",
    "payment_methods": "payment_method", "payment_intents": "payment_intent",
    "charges": "charge", "refunds": "refund", "invoices": "invoice",
    "invoice_items": "invoice_item", "subscriptions": "subscription",
    "checkout_sessions": "checkout_session", "coupons": "coupon",
    "payment_links": "payment_link", "disputes": "dispute", "events": "event",
    "balance_transactions": "balance_transaction",
}

_COUNTER_COLLECTIONS: dict[str, str] = {kind: name for name, kind in _COLLECTIONS.items()}

# ID prefix -> collection, so ``expand[]`` can resolve any reference field.
_BY_PREFIX: dict[str, str] = {
    "cus": "customers", "prod": "products", "price": "prices", "pm": "payment_methods",
    "pi": "payment_intents", "ch": "charges", "re": "refunds", "in": "invoices",
    "ii": "invoice_items", "sub": "subscriptions", "cs": "checkout_sessions",
    "cou": "coupons", "plink": "payment_links", "du": "disputes", "evt": "events",
    "txn": "balance_transactions",
}


def _now_unix() -> int:
    return int(time.time())


def _fresh_state() -> dict:
    return {
        "customers": {},            # cus_xxx -> dict
        "products": {},             # prod_xxx
        "prices": {},               # price_xxx
        "payment_methods": {},      # pm_xxx
        "payment_intents": {},      # pi_xxx
        "charges": {},              # ch_xxx
        "refunds": {},              # re_xxx
        "invoices": {},             # in_xxx
        "invoice_items": {},        # ii_xxx
        "subscriptions": {},        # sub_xxx
        "checkout_sessions": {},    # cs_test_xxx
        "coupons": {},              # cou_xxx (or a caller-supplied ID)
        "payment_links": {},        # plink_xxx
        "disputes": {},             # du_xxx
        "events": {},               # evt_xxx
        "balance_transactions": {},  # txn_xxx
        "balance": {
            "object": "balance",
            "available": [{"amount": 0, "currency": "usd", "source_types": {"card": 0}}],
            "pending": [{"amount": 0, "currency": "usd", "source_types": {"card": 0}}],
            "livemode": False,
        },
        "account": {
            "id": "acct_test_checkpoint",
            "object": "account",
            "business_profile": {"name": "Checkpoint Test Acct"},
            "charges_enabled": True,
            "country": "US",
            "default_currency": "usd",
            "details_submitted": True,
            "email": "owner@acme.test",
            "payouts_enabled": True,
            "type": "standard",
        },
        "_counters": dict.fromkeys(_PREFIXES, 0),
        "_idempotency": {},   # key -> {"path", "fingerprint", "status", "body"}
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers: identifiers ------------------------------------------------

def _next_id(kind: str) -> str:
    """Mint the next ID for ``kind``, skipping any a seed already used."""
    counters = STATE.setdefault("_counters", {})
    prefix = _PREFIXES[kind]
    # Sub-objects (line items, subscription items) have no collection of their own.
    taken = STATE.get(_COUNTER_COLLECTIONS.get(kind, ""), {})
    while True:
        counters[kind] = counters.get(kind, 0) + 1
        candidate = f"{prefix}_{counters[kind]:03d}"
        if candidate not in taken:
            return candidate


def _hash_body(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:32]


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


# --- helpers: request parsing --------------------------------------------

def _bracket_segments(key: str) -> list[str]:
    """Split a Stripe form key into path segments.

    ``items[0][price]`` -> ``["items", "0", "price"]``; ``expand[]`` ->
    ``["expand", ""]``; ``amount`` -> ``["amount"]``.
    """
    if "[" not in key:
        return [key]
    head, _, rest = key.partition("[")
    segs = [head]
    for chunk in rest.split("["):
        segs.append(chunk[:-1] if chunk.endswith("]") else chunk)
    return segs


def _assign_bracket_path(root: dict, key: str, value: Any) -> None:
    """Assign ``value`` into ``root`` following a Stripe bracket path.

    Nested objects become nested dicts (numeric indices are kept as string keys,
    matching how the twins read them); an empty trailing ``[]`` appends to a list
    so repeated ``expand[]`` params collect into one list.
    """
    segs = _bracket_segments(key)
    cur: Any = root
    for i in range(len(segs) - 1):
        seg, nxt = segs[i], segs[i + 1]
        if nxt == "":
            child = cur.get(seg) if isinstance(cur, dict) else None
            if not isinstance(child, list):
                child = []
                cur[seg] = child
            cur = child
        else:
            if not isinstance(cur, dict):
                return
            child = cur.get(seg)
            if not isinstance(child, dict):
                child = {}
                cur[seg] = child
            cur = child
    leaf = segs[-1]
    if leaf == "":
        if isinstance(cur, list):
            cur.append(value)
    elif isinstance(cur, dict):
        cur[leaf] = value


def _indices_to_lists(value: Any, *, key: str | None = None) -> Any:
    """Turn ``{"0": ..., "1": ...}`` (the SDKs' array encoding) back into a list.

    Metadata is left alone: its keys are the caller's, and may be numeric.
    """
    if isinstance(value, dict):
        if key != "metadata" and value and all(k.isdigit() for k in value):
            ordered = sorted(value.items(), key=lambda kv: int(kv[0]))
            return [_indices_to_lists(v) for _, v in ordered]
        return {k: _indices_to_lists(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_indices_to_lists(v) for v in value]
    return value


def _parse_raw(content_type: str, raw: bytes) -> dict:
    """Parse a Stripe request body: JSON, or form-encoded with bracket paths."""
    if not raw:
        return {}
    if "application/json" in content_type.lower():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    try:
        from urllib.parse import parse_qsl

        out: dict[str, Any] = {}
        for k, v in parse_qsl(raw.decode("utf-8"), keep_blank_values=True):
            _assign_bracket_path(out, k, v)
        return out
    except (UnicodeDecodeError, ValueError):
        # Some clients send JSON without a Content-Type.
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


async def _parse_body(request: Request) -> dict:
    """Parse JSON or x-www-form-urlencoded into a plain dict."""
    return _parse_raw(request.headers.get("content-type") or "", await request.body())


async def _params(request: Request) -> dict:
    """Every parameter of a request: query string first, body wins on conflict."""
    params: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        _assign_bracket_path(params, key, value)
    params.update(await _parse_body(request))
    return _indices_to_lists(params)


def _canonical(params: dict) -> bytes:
    return json.dumps(params, sort_keys=True, default=str).encode("utf-8")


# --- helpers: parameter coercion -----------------------------------------

# ``True`` also matches the integer 1, which JSON callers send for booleans.
_TRUE = {True, "true", "True", "1"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return value in _TRUE


def _as_int(value: Any) -> int | None:
    """Coerce a form value to int, or None when it is not an integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _merge_metadata(existing: dict, incoming: Any) -> dict:
    """Stripe merges metadata key by key; an empty value unsets one key, an
    empty ``metadata`` unsets them all."""
    if incoming in ("", None, {}):
        return {}
    if not isinstance(incoming, dict):
        return existing
    merged = dict(existing)
    for key, value in incoming.items():
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# --- helpers: errors -----------------------------------------------------

def stripe_error(status: int, message: str, *, type_: str = "invalid_request_error",
                 code: str | None = None, param: str | None = None,
                 decline_code: str | None = None) -> JSONResponse:
    err: dict[str, Any] = {"type": type_, "message": message}
    if code:
        err["code"] = code
    if param:
        err["param"] = param
    if decline_code:
        err["decline_code"] = decline_code
    return JSONResponse(status_code=status, content={"error": err})


def _missing(param: str) -> JSONResponse:
    return stripe_error(400, f"Missing required param: {param}.",
                        code="parameter_missing", param=param)


def _invalid_integer(param: str, value: Any) -> JSONResponse:
    return stripe_error(400, f"Invalid integer: {value}",
                        code="parameter_invalid_integer", param=param)


def _no_such(kind: str, ident: str, *, param: str | None = None) -> JSONResponse:
    return stripe_error(404, f"No such {kind}: '{ident}'", code="resource_missing",
                        param=param or kind)


def _unrecognized_url(method: str, path: str) -> JSONResponse:
    """Stripe's own 404 for a route it does not serve."""
    return stripe_error(
        404,
        f"Unrecognized request URL ({method.upper()}: {path}). Please see "
        "https://stripe.com/docs or we can help at https://support.stripe.com/.",
    )


# --- runtime: auth, faults, trace, control plane --------------------------

def _bootstrap_token() -> str:
    return os.environ.get("STRIPE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    """Stripe accepts the key as Bearer auth or as the Basic-auth username."""
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    scheme, _, rest = auth_header.partition(" ")
    scheme, rest = scheme.lower(), rest.strip()
    if scheme == "bearer":
        return rest or None
    if scheme == "basic":
        try:
            decoded = base64.b64decode(rest + "=" * (-len(rest) % 4)).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return decoded.partition(":")[0] or None
    return None


def _authenticate(request: Request) -> Response | None:
    token = _extract_token(request.headers.get("authorization"))
    if not token:
        return stripe_error(
            401,
            "You did not provide an API key. You need to provide your API key in the "
            "Authorization header, using Bearer auth.",
        )
    if TWIN.config.get("strict_auth") and token != _bootstrap_token():
        shown = token[:7] + "*" * 12 + token[-4:] if len(token) > 12 else token
        return stripe_error(401, f"Invalid API Key provided: {shown}")
    return None


def _error(kind: str, status: int, message: str) -> Response:
    """Shape the kit's uniform faults the way Stripe reports them."""
    if kind == "rate_limited":
        return JSONResponse(
            status_code=429,
            content={"error": {"type": "invalid_request_error", "code": "rate_limit",
                               "message": "Request rate limit exceeded."}},
            headers={"Stripe-Should-Retry": "true"},
        )
    if kind in ("forbidden", "read_only"):
        return stripe_error(403, message)
    return stripe_error(status, message, type_="api_error")


@app.middleware("http")
async def _idempotency(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Replay POSTs that carry an ``Idempotency-Key`` we have already answered.

    Registered before ``kit.install`` so the kit's pipeline stays outermost: a
    replay is still authenticated, fault-injected and traced like any call.
    """
    key = request.headers.get("idempotency-key")
    path = request.url.path
    if request.method != "POST" or not key or not path.startswith("/v1/"):
        return await call_next(request)

    raw = await request.body()
    fingerprint = _hash_body(_canonical(
        _indices_to_lists(_parse_raw(request.headers.get("content-type") or "", raw))))
    cached = STATE.setdefault("_idempotency", {}).get(key)
    if cached is not None:
        if cached["path"] != path:
            return stripe_error(
                400,
                f"Keys for idempotent requests can only be used with the same endpoint "
                f"they were first used with ('{cached['path']}' vs '{path}'). Try using a "
                f"key other than '{key}' if you meant to execute a different request.",
                type_="idempotency_error",
            )
        if cached["fingerprint"] != fingerprint:
            return stripe_error(
                400,
                f"Keys for idempotent requests can only be used with the same parameters "
                f"they were first used with. Try using a key other than '{key}' if you "
                f"meant to execute a different request.",
                type_="idempotency_error",
            )
        return JSONResponse(status_code=cached["status"], content=cached["body"],
                            headers={"Idempotent-Replayed": "true"})

    response = await call_next(request)
    body = b"".join([chunk async for chunk in response.body_iterator])
    if response.status_code < 400:
        # Only a request that actually executed is replayable — Stripe does not
        # cache rejected input.
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            STATE["_idempotency"][key] = {
                "path": path, "fingerprint": fingerprint,
                "status": response.status_code, "body": payload,
            }
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers,
                    media_type=response.media_type)


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """Answer routing failures the way Stripe does, not with FastAPI's ``detail``."""
    if exc.status_code in (404, 405):
        return _unrecognized_url(request.method, request.url.path)
    return stripe_error(exc.status_code, str(exc.detail), type_="api_error")


# --- classification and views --------------------------------------------

_SEGMENT_RESOURCES: dict[str, str] = {
    "customers": "customers", "products": "products", "prices": "prices",
    "payment_methods": "payment_methods", "payment_intents": "payment_intents",
    "charges": "charges", "refunds": "refunds", "invoices": "invoices",
    "invoiceitems": "invoice_items", "subscriptions": "subscriptions",
    "sessions": "checkout_sessions", "coupons": "coupons",
    "payment_links": "payment_links", "disputes": "disputes", "events": "events",
    "balance": "balance", "balance_transactions": "balance_transactions",
    "account": "account", "files": "files", "search": "search",
}

# Stripe changes state with POSTs to RPC-ish sub-paths; the verb says which op.
_RPC_OPS: dict[str, kit.Op] = {
    "confirm": "update", "capture": "update", "cancel": "update",
    "finalize": "update", "pay": "update", "send": "update", "void": "update",
    "mark_uncollectible": "update", "expire": "update", "attach": "update",
    "detach": "delete", "close": "update", "resume": "update",
}


def _classify(method: str, path: str, body: Any) -> tuple[kit.Op, str] | None:
    """Map a Stripe call to (op, resource) — POST means update or RPC, not only create."""
    segments = [s for s in path.strip("/").split("/") if s]
    if len(segments) < 2 or segments[0] != "v1":
        return None
    segments = segments[1:]
    family = segments[0]
    if family == "checkout":            # /v1/checkout/sessions[/{id}[/line_items]]
        segments = segments[1:] or ["sessions"]
        family = segments[0]
    resource = _SEGMENT_RESOURCES.get(family, family)
    tail = segments[1:]
    last = tail[-1] if tail else ""
    method = method.upper()
    if last == "search":
        return "read", resource
    if method == "GET":
        # A nested collection (``/v1/customers/{id}/payment_methods``) is a read
        # of the nested resource, not of its parent.
        return "read", _SEGMENT_RESOURCES.get(last, resource)
    if method == "DELETE":
        return "delete", resource
    if method != "POST":
        return None
    if last in _RPC_OPS:
        return _RPC_OPS[last], resource
    return ("update", resource) if tail else ("create", resource)


def _views(state: dict) -> dict[str, kit.View]:
    """Normalized collections for assertions: flat records with denormalized
    names, emails and amounts so a criterion does not have to join tables."""
    customers = state.get("customers") or {}
    prices = state.get("prices") or {}
    products = state.get("products") or {}

    def email_of(customer_id: Any) -> str | None:
        record = customers.get(customer_id) if isinstance(customer_id, str) else None
        return record.get("email") if isinstance(record, dict) else None

    def price_label(price_id: Any) -> str | None:
        price = prices.get(price_id) if isinstance(price_id, str) else None
        if not isinstance(price, dict):
            return None
        product = products.get(price.get("product"))
        return product.get("name") if isinstance(product, dict) else price.get("nickname")

    def records(name: str) -> list[dict]:
        return [r for r in (state.get(name) or {}).values() if isinstance(r, dict)]

    refunded_by_charge: dict[str, int] = {}
    for refund in records("refunds"):
        charge = refund.get("charge")
        if isinstance(charge, str):
            refunded_by_charge[charge] = refunded_by_charge.get(charge, 0) + (refund.get("amount") or 0)

    views: dict[str, kit.View] = {
        "customers": kit.View(
            [{"id": c.get("id"), "email": c.get("email"), "name": c.get("name"),
              "phone": c.get("phone"), "description": c.get("description"),
              "balance": c.get("balance", 0), "currency": c.get("currency"),
              "delinquent": c.get("delinquent", False), "created": c.get("created"),
              "metadata": c.get("metadata") or {}, "deleted": bool(c.get("deleted"))}
             for c in records("customers")],
            tombstone="deleted", nouns=("customer", "customers")),
        "products": kit.View(
            [{"id": p.get("id"), "name": p.get("name"), "description": p.get("description"),
              "active": p.get("active", True), "created": p.get("created"),
              "metadata": p.get("metadata") or {}, "deleted": bool(p.get("deleted"))}
             for p in records("products")],
            tombstone="deleted", nouns=("product", "products")),
        "prices": kit.View(
            [{"id": p.get("id"), "product": p.get("product"),
              "product_name": price_label(p.get("id")), "unit_amount": p.get("unit_amount"),
              "currency": p.get("currency"), "type": p.get("type"),
              "interval": (p.get("recurring") or {}).get("interval"),
              "active": p.get("active", True), "created": p.get("created")}
             for p in records("prices")],
            nouns=("price", "prices")),
        "payment_intents": kit.View(
            [{"id": p.get("id"), "amount": p.get("amount"), "currency": p.get("currency"),
              "status": p.get("status"), "customer": p.get("customer"),
              "customer_email": email_of(p.get("customer")),
              "charge": p.get("latest_charge"), "description": p.get("description"),
              "amount_refunded": refunded_by_charge.get(p.get("latest_charge") or "", 0),
              "capture_method": p.get("capture_method"), "created": p.get("created"),
              "metadata": p.get("metadata") or {}}
             for p in records("payment_intents")],
            nouns=("payment intent", "payment intents", "payment", "payments")),
        "charges": kit.View(
            [{"id": c.get("id"), "amount": c.get("amount"),
              "amount_refunded": c.get("amount_refunded", 0), "currency": c.get("currency"),
              "status": c.get("status"), "paid": c.get("paid", False),
              "refunded": c.get("refunded", False), "customer": c.get("customer"),
              "customer_email": email_of(c.get("customer")),
              "payment_intent": c.get("payment_intent"), "invoice": c.get("invoice"),
              "description": c.get("description"), "created": c.get("created")}
             for c in records("charges")],
            nouns=("charge", "charges")),
        "refunds": kit.View(
            [{"id": r.get("id"), "amount": r.get("amount"), "currency": r.get("currency"),
              "status": r.get("status"), "reason": r.get("reason"),
              "payment_intent": r.get("payment_intent"), "charge": r.get("charge"),
              "customer_email": email_of(
                  (state.get("payment_intents") or {}).get(r.get("payment_intent"), {}).get("customer")),
              "created": r.get("created"), "metadata": r.get("metadata") or {}}
             for r in records("refunds")],
            nouns=("refund", "refunds")),
        "invoices": kit.View(
            [{"id": i.get("id"), "number": i.get("number"), "status": i.get("status"),
              "customer": i.get("customer"), "customer_email": i.get("customer_email")
              or email_of(i.get("customer")), "amount_due": i.get("amount_due", 0),
              "amount_paid": i.get("amount_paid", 0), "total": i.get("total", 0),
              "currency": i.get("currency"), "description": i.get("description"),
              "subscription": i.get("subscription"), "sent": bool(i.get("sent_at")),
              "line_count": len(i.get("lines") or []), "created": i.get("created"),
              "deleted": bool(i.get("deleted")), "metadata": i.get("metadata") or {}}
             for i in records("invoices")],
            tombstone="deleted", nouns=("invoice", "invoices")),
        "invoice_items": kit.View(
            [{"id": i.get("id"), "customer": i.get("customer"),
              "customer_email": email_of(i.get("customer")), "amount": i.get("amount"),
              "currency": i.get("currency"), "description": i.get("description"),
              "invoice": i.get("invoice"), "created": i.get("created"),
              "deleted": bool(i.get("deleted"))}
             for i in records("invoice_items")],
            tombstone="deleted", nouns=("invoice item", "invoice items", "line item")),
        "subscriptions": kit.View(
            [{"id": s.get("id"), "customer": s.get("customer"),
              "customer_email": email_of(s.get("customer")), "status": s.get("status"),
              "cancel_at_period_end": s.get("cancel_at_period_end", False),
              "canceled": s.get("status") == "canceled",
              "price_ids": [i.get("price") for i in _as_list(s.get("items"))],
              "plans": [price_label(i.get("price")) for i in _as_list(s.get("items"))],
              "amount": sum(((prices.get(i.get("price")) or {}).get("unit_amount") or 0)
                            * (i.get("quantity") or 1) for i in _as_list(s.get("items"))),
              "current_period_end": s.get("current_period_end"),
              "created": s.get("created"), "metadata": s.get("metadata") or {}}
             for s in records("subscriptions")],
            nouns=("subscription", "subscriptions")),
        "checkout_sessions": kit.View(
            [{"id": s.get("id"), "mode": s.get("mode"), "status": s.get("status"),
              "payment_status": s.get("payment_status"),
              "amount_total": s.get("amount_total"), "currency": s.get("currency"),
              "customer": s.get("customer"),
              "customer_email": s.get("customer_email") or email_of(s.get("customer")),
              "url": s.get("url"), "created": s.get("created")}
             for s in records("checkout_sessions")],
            nouns=("checkout session", "checkout sessions", "session", "sessions")),
        "payment_methods": kit.View(
            [{"id": m.get("id"), "type": m.get("type"), "customer": m.get("customer"),
              "customer_email": email_of(m.get("customer")),
              "brand": (m.get("card") or {}).get("brand"),
              "last4": (m.get("card") or {}).get("last4"), "created": m.get("created"),
              "attached": bool(m.get("customer"))}
             for m in records("payment_methods")],
            nouns=("payment method", "payment methods", "card", "cards")),
        "coupons": kit.View(
            [{"id": c.get("id"), "name": c.get("name"), "percent_off": c.get("percent_off"),
              "amount_off": c.get("amount_off"), "duration": c.get("duration"),
              "valid": c.get("valid", True), "created": c.get("created"),
              "deleted": bool(c.get("deleted"))}
             for c in records("coupons")],
            tombstone="deleted", nouns=("coupon", "coupons")),
        "payment_links": kit.View(
            [{"id": p.get("id"), "url": p.get("url"), "active": p.get("active", True),
              "created": p.get("created"),
              "price_ids": [i.get("price") for i in _as_list(p.get("line_items"))]}
             for p in records("payment_links")],
            nouns=("payment link", "payment links")),
        "disputes": kit.View(
            [{"id": d.get("id"), "amount": d.get("amount"), "currency": d.get("currency"),
              "status": d.get("status"), "reason": d.get("reason"),
              "charge": d.get("charge"), "payment_intent": d.get("payment_intent"),
              "created": d.get("created")}
             for d in records("disputes")],
            nouns=("dispute", "disputes", "chargeback", "chargebacks")),
        "events": kit.View(
            [{"id": e.get("id"), "type": e.get("type"), "created": e.get("created"),
              "object_id": ((e.get("data") or {}).get("object") or {}).get("id"),
              "object_type": ((e.get("data") or {}).get("object") or {}).get("object")}
             for e in records("events")],
            nouns=("event", "events")),
        "balance_transactions": kit.View(
            [{"id": t.get("id"), "amount": t.get("amount"), "net": t.get("net"),
              "fee": t.get("fee"), "currency": t.get("currency"), "type": t.get("type"),
              "source": t.get("source"), "status": t.get("status"),
              "description": t.get("description"), "created": t.get("created")}
             for t in records("balance_transactions")],
            nouns=("balance transaction", "balance transactions", "payout", "payouts")),
    }
    # The balance is one record per currency, so criteria can talk about "the balance".
    balance = state.get("balance") or {}
    pending = {b.get("currency"): b.get("amount", 0) for b in balance.get("pending") or []}
    views["balance"] = kit.View(
        [{"currency": b.get("currency"), "available": b.get("amount", 0),
          "pending": pending.get(b.get("currency"), 0)}
         for b in balance.get("available") or []],
        key="currency", nouns=("balance", "account balance"))
    return views


# --- objects: defaults and seed normalization ----------------------------

# Fields every object of a kind carries. Seeds are terse, so records are filled
# in on load: an SDK reading ``customer.metadata`` must not see it missing.
_OBJECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "customers": {
        "object": "customer", "address": None, "balance": 0, "currency": "usd",
        "default_source": None, "delinquent": False, "description": None,
        "discount": None, "email": None, "invoice_prefix": None,
        "invoice_settings": {"custom_fields": None, "default_payment_method": None,
                             "footer": None},
        "livemode": False, "metadata": {}, "name": None, "next_invoice_sequence": 1,
        "phone": None, "preferred_locales": [], "shipping": None, "tax_exempt": "none",
    },
    "products": {
        "object": "product", "active": True, "description": None, "images": [],
        "livemode": False, "metadata": {}, "name": "", "shippable": None,
        "statement_descriptor": None, "tax_code": None, "type": "service",
        "unit_label": None, "url": None,
    },
    "prices": {
        "object": "price", "active": True, "billing_scheme": "per_unit", "currency": "usd",
        "livemode": False, "lookup_key": None, "metadata": {}, "nickname": None,
        "product": None, "recurring": None, "tax_behavior": "unspecified",
        "tiers_mode": None, "transform_quantity": None, "type": "one_time",
        "unit_amount": None,
    },
    "payment_methods": {
        "object": "payment_method", "billing_details": {"address": None, "email": None,
                                                        "name": None, "phone": None},
        "customer": None, "livemode": False, "metadata": {}, "type": "card",
    },
    "payment_intents": {
        "object": "payment_intent", "amount_capturable": 0, "amount_received": 0,
        "automatic_payment_methods": None, "canceled_at": None, "cancellation_reason": None,
        "capture_method": "automatic", "confirmation_method": "automatic", "currency": "usd",
        "customer": None, "description": None, "invoice": None, "last_payment_error": None,
        "latest_charge": None, "livemode": False, "metadata": {}, "next_action": None,
        "payment_method": None, "payment_method_types": ["card"], "receipt_email": None,
        "setup_future_usage": None, "shipping": None, "statement_descriptor": None,
        "status": "requires_payment_method", "transfer_data": None, "transfer_group": None,
    },
    "charges": {
        "object": "charge", "amount_captured": 0, "amount_refunded": 0,
        "balance_transaction": None, "billing_details": {"address": None, "email": None,
                                                         "name": None, "phone": None},
        "captured": True, "currency": "usd", "customer": None, "description": None,
        "disputed": False, "failure_code": None, "failure_message": None, "invoice": None,
        "livemode": False, "metadata": {}, "paid": True, "payment_intent": None,
        "payment_method": None, "receipt_url": None, "refunded": False, "status": "succeeded",
    },
    "refunds": {
        "object": "refund", "balance_transaction": None, "charge": None, "currency": "usd",
        "livemode": False, "metadata": {}, "payment_intent": None, "reason": None,
        "receipt_number": None, "status": "succeeded",
    },
    "invoices": {
        "object": "invoice", "account_country": "US", "account_name": "Checkpoint Test Acct",
        "amount_due": 0, "amount_paid": 0, "amount_remaining": 0, "attempt_count": 0,
        "attempted": False, "auto_advance": True, "billing_reason": "manual",
        "collection_method": "charge_automatically", "currency": "usd", "customer": None,
        "customer_email": None, "customer_name": None, "default_payment_method": None,
        "description": None, "discounts": [], "due_date": None, "ending_balance": None,
        "footer": None, "hosted_invoice_url": None, "invoice_pdf": None, "lines": [],
        "livemode": False, "metadata": {}, "next_payment_attempt": None, "number": None,
        "paid": False, "paid_out_of_band": False, "payment_intent": None, "charge": None,
        "period_end": None, "period_start": None, "receipt_number": None, "sent_at": None,
        "starting_balance": 0, "statement_descriptor": None, "status": "draft",
        "status_transitions": {"finalized_at": None, "marked_uncollectible_at": None,
                               "paid_at": None, "voided_at": None},
        "subscription": None, "subtotal": 0, "tax": None, "total": 0,
        "webhooks_delivered_at": None,
    },
    "invoice_items": {
        "object": "invoiceitem", "amount": 0, "currency": "usd", "customer": None,
        "description": None, "discountable": True, "invoice": None, "livemode": False,
        "metadata": {}, "period": None, "price": None, "proration": False, "quantity": 1,
        "unit_amount": None,
    },
    "subscriptions": {
        "object": "subscription", "application": None, "billing_cycle_anchor": None,
        "cancel_at": None, "cancel_at_period_end": False, "canceled_at": None,
        "collection_method": "charge_automatically", "currency": "usd", "customer": None,
        "days_until_due": None, "default_payment_method": None, "description": None,
        "discount": None, "ended_at": None, "items": [], "latest_invoice": None,
        "livemode": False, "metadata": {}, "pause_collection": None, "schedule": None,
        "start_date": None, "status": "active", "trial_end": None, "trial_start": None,
    },
    "checkout_sessions": {
        "object": "checkout.session", "amount_subtotal": 0, "amount_total": 0,
        "cancel_url": None, "client_reference_id": None, "currency": "usd", "customer": None,
        "customer_email": None, "expires_at": None, "invoice": None, "line_items": [],
        "livemode": False, "metadata": {}, "mode": "payment", "payment_intent": None,
        "payment_link": None, "payment_method_types": ["card"], "payment_status": "unpaid",
        "status": "open", "subscription": None, "success_url": None,
        "total_details": {"amount_discount": 0, "amount_shipping": 0, "amount_tax": 0},
        "ui_mode": "hosted_page", "url": None,
    },
    "coupons": {
        "object": "coupon", "amount_off": None, "currency": None, "duration": "once",
        "duration_in_months": None, "livemode": False, "max_redemptions": None,
        "metadata": {}, "name": None, "percent_off": None, "redeem_by": None,
        "times_redeemed": 0, "valid": True,
    },
    "payment_links": {
        "object": "payment_link", "active": True, "allow_promotion_codes": False,
        "currency": "usd", "line_items": [], "livemode": False, "metadata": {},
        "payment_method_types": ["card"], "url": None,
    },
    "disputes": {
        "object": "dispute", "amount": 0, "charge": None, "currency": "usd", "evidence": {},
        "evidence_details": {"due_by": None, "has_evidence": False, "past_due": False,
                             "submission_count": 0},
        "is_charge_refundable": True, "livemode": False, "metadata": {},
        "payment_intent": None, "reason": "general", "status": "warning_needs_response",
    },
    "events": {
        "object": "event", "api_version": API_VERSION, "data": {"object": {}},
        "livemode": False, "pending_webhooks": 0, "request": {"id": None,
                                                              "idempotency_key": None},
        "type": "",
    },
    "balance_transactions": {
        "object": "balance_transaction", "amount": 0, "available_on": None, "currency": "usd",
        "description": None, "exchange_rate": None, "fee": 0, "fee_details": [], "net": 0,
        "reporting_category": "charge", "source": None, "status": "available", "type": "charge",
    },
}


def _normalize_state(state: dict) -> None:
    """Fill seeded records out to full Stripe objects.

    Seeds carry only the fields a human cares about; SDK users read the rest, and
    the counters have to continue past whatever IDs a seed already used.
    """
    for collection, defaults in _OBJECT_DEFAULTS.items():
        records = state.get(collection)
        if not isinstance(records, dict):
            continue
        for ident, record in list(records.items()):
            if not isinstance(record, dict):
                continue
            merged = {**_copy(defaults), **record}
            merged["id"] = record.get("id", ident)
            merged.setdefault("created", _now_unix())
            if collection == "subscriptions":
                merged["items"] = _as_list(
                    merged["items"].get("data") if isinstance(merged["items"], dict)
                    else merged["items"])
                merged.setdefault("start_date", merged["created"])
            if collection == "invoices" and isinstance(merged.get("lines"), dict):
                merged["lines"] = _as_list(merged["lines"].get("data"))
            records[ident] = merged
    counters = state.setdefault("_counters", {})
    for collection, kind in _COLLECTIONS.items():
        highest = 0
        for ident in state.get(collection) or {}:
            suffix = str(ident).rsplit("_", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        counters[kind] = max(counters.get(kind, 0), highest)
    _backfill_charges(state)


def _backfill_charges(state: dict) -> None:
    """Give every seeded payment that went through the charge Stripe would have
    made, so refunds and reporting have something to work against."""
    for intent in list((state.get("payment_intents") or {}).values()):
        if intent.get("status") not in ("succeeded", "requires_capture"):
            continue
        if intent.get("latest_charge"):
            continue
        captured = intent["status"] == "succeeded"
        charge = _create("charges", {
            "amount": intent.get("amount", 0),
            "amount_captured": intent.get("amount", 0) if captured else 0,
            "captured": captured,
            "created": intent.get("created"),
            "currency": intent.get("currency", "usd"),
            "customer": intent.get("customer"),
            "description": intent.get("description"),
            "payment_intent": intent["id"],
            "payment_method": intent.get("payment_method"),
            "payment_method_details": {"card": _card_details("visa", "4242"), "type": "card"},
        })
        charge["receipt_url"] = f"{DASHBOARD_HOST}/receipts/{charge['id']}"
        intent["latest_charge"] = charge["id"]
        if captured:
            intent["amount_received"] = intent.get("amount", 0)
        else:
            intent["amount_capturable"] = intent.get("amount", 0)


TWIN = kit.install(app, kit.Twin(
    name="stripe",
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    classify=_classify,
    views=_views,
    after_seed=_normalize_state,
))


# --- objects: creation, rendering, expansion -----------------------------

def _create(collection: str, fields: dict, *, ident: str | None = None) -> dict:
    """Store a new object of ``collection``, filled out with its defaults."""
    record = {**_copy(_OBJECT_DEFAULTS.get(collection, {})), **fields}
    record["id"] = ident or _next_id(_COLLECTIONS[collection])
    record.setdefault("created", _now_unix())
    STATE[collection][record["id"]] = record
    return record


def _get(collection: str, ident: Any) -> dict | None:
    record = (STATE.get(collection) or {}).get(ident) if isinstance(ident, str) else None
    return record if isinstance(record, dict) else None


def _lookup(ident: Any) -> dict | None:
    """The stored object an ID refers to, whichever collection it lives in."""
    if not isinstance(ident, str):
        return None
    return _get(_BY_PREFIX.get(ident.split("_", 1)[0], ""), ident)


def _list_obj(data: list, url: str, has_more: bool = False) -> dict:
    return {"object": "list", "data": data, "has_more": has_more, "url": url}


def _deleted(record: dict) -> dict:
    return {"id": record["id"], "object": record["object"], "deleted": True}


def _render(record: dict) -> dict:
    """A stored object as Stripe puts it on the wire."""
    out = _copy(record)
    kind = out.get("object")
    if kind == "subscription":
        items = [_render_subscription_item(i, record) for i in _as_list(record.get("items"))]
        out["items"] = _list_obj(items, f"/v1/subscription_items?subscription={record['id']}")
    elif kind == "invoice":
        lines = [_render_invoice_line(line) for line in _as_list(record.get("lines"))]
        out["lines"] = _list_obj(lines, f"/v1/invoices/{record['id']}/lines")
    elif kind == "charge":
        refunds = [_render(r) for r in _newest_first(
            r for r in STATE["refunds"].values() if r.get("charge") == record["id"])]
        out["refunds"] = _list_obj(refunds, f"/v1/charges/{record['id']}/refunds")
    elif kind == "checkout.session":
        # Checkout line items come back only when ``expand`` asks for them.
        out.pop("line_items", None)
    return out


def _render_subscription_item(item: dict, subscription: dict) -> dict:
    price = _get("prices", item.get("price"))
    return {
        "id": item.get("id") or _next_id("subscription_item"),
        "object": "subscription_item",
        "created": item.get("created", subscription.get("created")),
        "current_period_end": subscription.get("current_period_end"),
        "current_period_start": subscription.get("current_period_start"),
        "metadata": item.get("metadata") or {},
        "price": _render(price) if price else item.get("price"),
        "quantity": item.get("quantity", 1),
        "subscription": subscription.get("id"),
    }


def _render_invoice_line(line: dict) -> dict:
    price = _get("prices", line.get("price"))
    return {
        "id": line.get("id"),
        "object": "line_item",
        "amount": line.get("amount", 0),
        "currency": line.get("currency", "usd"),
        "description": line.get("description"),
        "discountable": True,
        "invoice_item": line.get("invoice_item"),
        "livemode": False,
        "metadata": line.get("metadata") or {},
        "period": line.get("period"),
        "price": _render(price) if price else None,
        "proration": False,
        "quantity": line.get("quantity", 1),
        "subscription": line.get("subscription"),
        "type": line.get("type", "invoiceitem"),
    }


_MISSING = object()


def _includable(node: dict, field: str) -> Any:
    """Fields Stripe leaves out of a response until ``expand`` asks for them."""
    if node.get("object") == "checkout.session" and field == "line_items":
        session = _get("checkout_sessions", node.get("id"))
        if session is None:
            return None
        return _list_obj(_session_line_items(session),
                         f"/v1/checkout/sessions/{session['id']}/line_items")
    return None


def _expand(node: Any, paths: Any) -> Any:
    """Replace the ID in each ``expand[]`` path with the object it points at."""
    for path in _as_list(paths):
        _expand_path(node, [s for s in str(path).split(".") if s])
    return node


def _expand_path(node: Any, segments: list[str]) -> None:
    if not segments:
        return
    if isinstance(node, list):
        for item in node:
            _expand_path(item, segments)
        return
    if not isinstance(node, dict):
        return
    segment, rest = segments[0], segments[1:]
    if segment == "data" and isinstance(node.get("data"), list):
        for item in node["data"]:
            _expand_path(item, rest)
        return
    value = node.get(segment, _MISSING)
    if value is _MISSING or value is None:
        included = _includable(node, segment)
        if included is None:
            return
        node[segment] = value = included
    if isinstance(value, str):
        target = _lookup(value)
        if target is None:
            return
        node[segment] = value = _render(target)
    if rest:
        _expand_path(value, rest)


def _respond(record: dict, params: dict | None = None) -> JSONResponse:
    return JSONResponse(_expand(_render(record), (params or {}).get("expand")))


# --- objects: events and money -------------------------------------------

def _emit(event_type: str, record: dict) -> dict:
    """Record the event Stripe would emit for a mutation, so runs are auditable."""
    return _create("events", {"type": event_type, "data": {"object": _render(record)}})


def _balance_bucket(kind: str, currency: str) -> dict:
    for bucket in STATE["balance"].setdefault(kind, []):
        if bucket.get("currency") == currency:
            return bucket
    bucket = {"amount": 0, "currency": currency, "source_types": {"card": 0}}
    STATE["balance"][kind].append(bucket)
    return bucket


def _balance_txn(amount: int, currency: str, kind: str, source: str,
                 description: str | None = None) -> dict:
    """Move money in the account balance and record what explains the move."""
    bucket = _balance_bucket("available", currency)
    bucket["amount"] += amount
    if isinstance(bucket.get("source_types"), dict):
        bucket["source_types"]["card"] = bucket["amount"]
    return _create("balance_transactions", {
        "amount": amount, "available_on": _now_unix(), "currency": currency,
        "description": description, "net": amount, "source": source,
        "reporting_category": kind, "type": kind,
    })


# --- lists, filters, pagination ------------------------------------------

def _newest_first(records: Iterable[dict]) -> list[dict]:
    """Stripe returns lists newest first; insertion order breaks same-second ties."""
    indexed = list(enumerate(records))
    indexed.sort(key=lambda pair: (pair[1].get("created") or 0, pair[0]), reverse=True)
    return [record for _, record in indexed]


def _matches_created(record: dict, spec: Any) -> bool:
    """A ``created`` filter is either a timestamp or a {gt,gte,lt,lte} range."""
    created = record.get("created") or 0
    if isinstance(spec, dict):
        for op, raw in spec.items():
            bound = _as_int(raw)
            if bound is None:
                continue
            if op == "gt" and not created > bound:
                return False
            if op == "gte" and not created >= bound:
                return False
            if op == "lt" and not created < bound:
                return False
            if op == "lte" and not created <= bound:
                return False
        return True
    exact = _as_int(spec)
    return exact is None or created == exact


def _filtered(records: Iterable[dict], params: dict, fields: Iterable[str]) -> list[dict]:
    """Apply the list filters Stripe documents for a collection."""
    out = [r for r in records if not r.get("deleted")]
    for field in fields:
        if field not in params or params[field] in (None, ""):
            continue
        wanted = params[field]
        if field == "created":
            out = [r for r in out if _matches_created(r, wanted)]
        elif field in ("active", "shippable"):
            out = [r for r in out if bool(r.get(field)) is _as_bool(wanted)]
        else:
            out = [r for r in out if r.get(field) == wanted]
    return out


def _paginate(records: Iterable[dict], params: dict, url: str) -> JSONResponse:
    """One Stripe list page: newest first, with real has_more/cursor semantics."""
    limit = _as_int(params.get("limit", 10))
    if limit is None:
        return _invalid_integer("limit", params.get("limit"))
    if not 1 <= limit <= 100:
        return stripe_error(
            400,
            f"This value must be greater than or equal to 1 and less than or equal to 100 "
            f"(it currently is '{limit}').",
            code="parameter_invalid_integer", param="limit")

    ordered = _newest_first(records)
    ids = [r.get("id") for r in ordered]
    cursor = params.get("starting_after") or params.get("ending_before")
    if cursor and cursor not in ids:
        return stripe_error(400, f"No such object: '{cursor}'", code="resource_missing",
                            param="starting_after" if params.get("starting_after")
                            else "ending_before")
    if params.get("starting_after"):
        start = ids.index(params["starting_after"]) + 1
        window, has_more = ordered[start:start + limit], len(ordered) > start + limit
    elif params.get("ending_before"):
        end = ids.index(params["ending_before"])
        start = max(0, end - limit)
        window, has_more = ordered[start:end], start > 0
    else:
        window, has_more = ordered[:limit], len(ordered) > limit
    payload = _list_obj([_render(r) for r in window], url, has_more)
    return JSONResponse(_expand(payload, params.get("expand")))


# --- search --------------------------------------------------------------

# Fields Stripe lets a search query name, per resource.
_SEARCH_FIELDS: dict[str, set[str]] = {
    "customers": {"created", "email", "metadata", "name", "phone"},
    "charges": {"amount", "created", "currency", "customer", "disputed", "metadata",
                "refunded", "status"},
    "invoices": {"created", "currency", "customer", "metadata", "number",
                 "receipt_number", "status", "subscription", "total"},
    "payment_intents": {"amount", "created", "currency", "customer", "metadata", "status"},
    "prices": {"active", "currency", "lookup_key", "metadata", "product", "type"},
    "products": {"active", "description", "metadata", "name", "shippable", "url"},
    "subscriptions": {"canceled_at", "created", "metadata", "status"},
}

_CLAUSE_RE = re.compile(
    r"""(?P<neg>-)?(?P<field>[A-Za-z_][\w.]*)
        (?:\[\s*(?P<quote>["'])(?P<key>[^"']*)(?P=quote)\s*\])?\s*
        (?P<op>>=|<=|[:~><=])\s*
        (?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\S+)""",
    re.VERBOSE)


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw.replace('\\"', '"').replace("\\'", "'")


def _field_value(record: dict, field: str, key: str | None) -> Any:
    if key is not None:
        return (record.get(field) or {}).get(key)
    value: Any = record
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _clause_matches(record: dict, clause: dict) -> bool:
    actual = _field_value(record, clause["field"], clause["key"])
    wanted, op = clause["value"], clause["op"]
    if wanted.lower() == "null" and op == ":":
        hit = actual in (None, "", [], {})
    elif op == "~":
        hit = isinstance(actual, str) and wanted.lower() in actual.lower()
    elif op in (">", "<", ">=", "<=", "="):
        left, right = _as_int(actual), _as_int(wanted)
        if left is None or right is None:
            hit = False
        else:
            hit = {">": left > right, "<": left < right, ">=": left >= right,
                   "<=": left <= right, "=": left == right}[op]
    else:
        hit = str(actual).lower() == wanted.lower()
    return not hit if clause["negated"] else hit


def _search(records: Iterable[dict], params: dict, url: str, resource: str) -> JSONResponse:
    """Stripe's search: a small query language over one resource, page-token paged."""
    query = params.get("query")
    if not query:
        return _missing("query")
    allowed = _SEARCH_FIELDS.get(resource, set())
    clauses: list[dict] = []
    for match in _CLAUSE_RE.finditer(str(query)):
        field = match.group("field")
        root = field.split(".")[0]
        if root not in allowed:
            return stripe_error(
                400,
                f"Invalid search query: field '{field}' is not a valid field to query on "
                f"{resource}. Valid fields are: {', '.join(sorted(allowed))}.",
                param="query")
        clauses.append({"field": field, "key": match.group("key"), "op": match.group("op"),
                        "value": _unquote(match.group("value")),
                        "negated": bool(match.group("neg"))})
    if not clauses:
        return stripe_error(400, f"Invalid search query: '{query}'.", param="query")
    combine = any if re.search(r"\bor\b", str(query), re.IGNORECASE) else all

    matched = [r for r in _newest_first(records)
               if not r.get("deleted") and combine(_clause_matches(r, c) for c in clauses)]
    limit = _as_int(params.get("limit", 10)) or 10
    page = _as_int(params.get("page")) or 0
    window = matched[page:page + limit]
    has_more = len(matched) > page + limit
    payload = {
        "object": "search_result",
        "data": [_render(r) for r in window],
        "has_more": has_more,
        "next_page": str(page + limit) if has_more else None,
        "url": url,
    }
    return JSONResponse(_expand(payload, params.get("expand")))


# --- customers -----------------------------------------------------------

_CUSTOMER_FIELDS = ("address", "balance", "description", "email", "invoice_settings",
                    "name", "phone", "preferred_locales", "shipping", "tax_exempt",
                    "default_source")


@app.post("/v1/customers")
async def create_customer(request: Request):
    params = await _params(request)
    customer = _create("customers", {
        "email": params.get("email"),
        "name": params.get("name"),
        "description": params.get("description"),
        "phone": params.get("phone"),
        "address": params.get("address"),
        "shipping": params.get("shipping"),
        "currency": params.get("currency", "usd"),
        "balance": _as_int(params.get("balance")) or 0,
        "metadata": _merge_metadata({}, params.get("metadata")),
        "invoice_settings": {"custom_fields": None, "footer": None,
                             "default_payment_method": (params.get("invoice_settings") or {})
                             .get("default_payment_method")},
    })
    customer["invoice_prefix"] = customer["id"].replace("_", "").upper()[:8]
    if params.get("payment_method"):
        attached = _payment_method(params["payment_method"])
        if attached is not None:
            attached["customer"] = customer["id"]
    _emit("customer.created", customer)
    return _respond(customer, params)


@app.get("/v1/customers")
async def list_customers(request: Request):
    params = await _params(request)
    records = _filtered(STATE["customers"].values(), params, ("email", "created"))
    return _paginate(records, params, "/v1/customers")


@app.get("/v1/customers/search")
async def search_customers(request: Request):
    params = await _params(request)
    return _search(STATE["customers"].values(), params, "/v1/customers/search", "customers")


@app.get("/v1/customers/{customer_id}")
async def retrieve_customer(customer_id: str, request: Request):
    params = await _params(request)
    customer = _get("customers", customer_id)
    if customer is None:
        return _no_such("customer", customer_id)
    # Stripe still answers for a deleted customer, with the tombstone.
    if customer.get("deleted"):
        return JSONResponse(_deleted(customer))
    return _respond(customer, params)


@app.post("/v1/customers/{customer_id}")
async def update_customer(customer_id: str, request: Request):
    params = await _params(request)
    customer = _get("customers", customer_id)
    if customer is None or customer.get("deleted"):
        return _no_such("customer", customer_id)
    for field in _CUSTOMER_FIELDS:
        if field in params:
            value = params[field]
            customer[field] = _as_int(value) if field == "balance" else value
    if "metadata" in params:
        customer["metadata"] = _merge_metadata(customer.get("metadata") or {},
                                               params["metadata"])
    _emit("customer.updated", customer)
    return _respond(customer, params)


@app.delete("/v1/customers/{customer_id}")
async def delete_customer(customer_id: str):
    customer = _get("customers", customer_id)
    if customer is None or customer.get("deleted"):
        return _no_such("customer", customer_id)
    customer["deleted"] = True
    # Deleting a customer cancels whatever they were subscribed to.
    for subscription in STATE["subscriptions"].values():
        if subscription.get("customer") == customer_id and subscription.get("status") not in (
                "canceled", "incomplete_expired"):
            subscription["status"] = "canceled"
            subscription["canceled_at"] = subscription["ended_at"] = _now_unix()
    _emit("customer.deleted", customer)
    return JSONResponse(_deleted(customer))


@app.get("/v1/customers/{customer_id}/payment_methods")
async def list_customer_payment_methods(customer_id: str, request: Request):
    params = await _params(request)
    if _get("customers", customer_id) is None:
        return _no_such("customer", customer_id)
    records = [m for m in STATE["payment_methods"].values() if m.get("customer") == customer_id]
    if params.get("type"):
        records = [m for m in records if m.get("type") == params["type"]]
    return _paginate(records, params, f"/v1/customers/{customer_id}/payment_methods")


@app.get("/v1/customers/{customer_id}/subscriptions")
async def list_customer_subscriptions(customer_id: str, request: Request):
    params = await _params(request)
    if _get("customers", customer_id) is None:
        return _no_such("customer", customer_id)
    records = [s for s in STATE["subscriptions"].values() if s.get("customer") == customer_id]
    return _paginate(records, params, f"/v1/customers/{customer_id}/subscriptions")


# --- products and prices -------------------------------------------------

@app.post("/v1/products")
async def create_product(request: Request):
    params = await _params(request)
    name = params.get("name")
    if not name:
        return _missing("name")
    product = _create("products", {
        "name": name,
        "description": params.get("description"),
        "active": _as_bool(params.get("active"), True),
        "images": _as_list(params.get("images")),
        "url": params.get("url"),
        "shippable": params.get("shippable"),
        "unit_label": params.get("unit_label"),
        "metadata": _merge_metadata({}, params.get("metadata")),
    }, ident=params.get("id"))
    _emit("product.created", product)
    return _respond(product, params)


@app.get("/v1/products")
async def list_products(request: Request):
    params = await _params(request)
    records = _filtered(STATE["products"].values(), params,
                        ("active", "shippable", "url", "created"))
    if params.get("ids"):
        wanted = set(_as_list(params["ids"]))
        records = [p for p in records if p["id"] in wanted]
    return _paginate(records, params, "/v1/products")


@app.get("/v1/products/search")
async def search_products(request: Request):
    params = await _params(request)
    return _search(STATE["products"].values(), params, "/v1/products/search", "products")


@app.get("/v1/products/{product_id}")
async def retrieve_product(product_id: str, request: Request):
    params = await _params(request)
    product = _get("products", product_id)
    if product is None or product.get("deleted"):
        return _no_such("product", product_id)
    return _respond(product, params)


@app.post("/v1/products/{product_id}")
async def update_product(product_id: str, request: Request):
    params = await _params(request)
    product = _get("products", product_id)
    if product is None or product.get("deleted"):
        return _no_such("product", product_id)
    for field in ("name", "description", "url", "unit_label", "shippable",
                  "statement_descriptor", "tax_code"):
        if field in params:
            product[field] = params[field]
    if "active" in params:
        product["active"] = _as_bool(params["active"])
    if "images" in params:
        product["images"] = _as_list(params["images"])
    if "metadata" in params:
        product["metadata"] = _merge_metadata(product.get("metadata") or {}, params["metadata"])
    _emit("product.updated", product)
    return _respond(product, params)


@app.delete("/v1/products/{product_id}")
async def delete_product(product_id: str):
    product = _get("products", product_id)
    if product is None or product.get("deleted"):
        return _no_such("product", product_id)
    if any(p.get("product") == product_id for p in STATE["prices"].values()):
        return stripe_error(
            400,
            "You cannot delete a product that has prices associated with it. Instead, "
            "either deactivate the product or delete its prices first.")
    product["deleted"] = True
    _emit("product.deleted", product)
    return JSONResponse(_deleted(product))


@app.post("/v1/prices")
async def create_price(request: Request):
    params = await _params(request)
    product_id = params.get("product")
    product_data = params.get("product_data")
    if not product_id and not product_data:
        return _missing("product")
    if not product_id:
        product = _create("products", {"name": (product_data or {}).get("name") or "",
                                       "metadata": {}})
        product_id = product["id"]
        _emit("product.created", product)
    elif _get("products", product_id) is None:
        return _no_such("product", product_id, param="product")
    if params.get("unit_amount") is None and params.get("unit_amount_decimal") is None:
        return _missing("unit_amount")
    amount = _as_int(params.get("unit_amount", params.get("unit_amount_decimal")))
    if amount is None:
        return _invalid_integer("unit_amount", params.get("unit_amount"))
    recurring = params.get("recurring") or None
    if isinstance(recurring, dict):
        recurring = {"interval": recurring.get("interval", "month"),
                     "interval_count": _as_int(recurring.get("interval_count")) or 1,
                     "usage_type": recurring.get("usage_type", "licensed"),
                     "trial_period_days": _as_int(recurring.get("trial_period_days"))}
    price = _create("prices", {
        "product": product_id,
        "unit_amount": amount,
        "unit_amount_decimal": str(amount),
        "currency": params.get("currency", "usd"),
        "active": _as_bool(params.get("active"), True),
        "nickname": params.get("nickname"),
        "lookup_key": params.get("lookup_key"),
        "type": "recurring" if recurring else "one_time",
        "recurring": recurring,
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    _emit("price.created", price)
    return _respond(price, params)


@app.get("/v1/prices")
async def list_prices(request: Request):
    params = await _params(request)
    records = _filtered(STATE["prices"].values(), params,
                        ("product", "active", "currency", "type", "created"))
    if params.get("lookup_keys"):
        wanted = set(_as_list(params["lookup_keys"]))
        records = [p for p in records if p.get("lookup_key") in wanted]
    return _paginate(records, params, "/v1/prices")


@app.get("/v1/prices/search")
async def search_prices(request: Request):
    params = await _params(request)
    return _search(STATE["prices"].values(), params, "/v1/prices/search", "prices")


@app.get("/v1/prices/{price_id}")
async def retrieve_price(price_id: str, request: Request):
    params = await _params(request)
    price = _get("prices", price_id)
    if price is None:
        return _no_such("price", price_id)
    return _respond(price, params)


@app.post("/v1/prices/{price_id}")
async def update_price(price_id: str, request: Request):
    params = await _params(request)
    price = _get("prices", price_id)
    if price is None:
        return _no_such("price", price_id)
    for field in ("nickname", "lookup_key", "tax_behavior"):
        if field in params:
            price[field] = params[field]
    if "active" in params:
        price["active"] = _as_bool(params["active"])
    if "metadata" in params:
        price["metadata"] = _merge_metadata(price.get("metadata") or {}, params["metadata"])
    _emit("price.updated", price)
    return _respond(price, params)


# --- payment methods -----------------------------------------------------

# Stripe's shared test payment methods, which agents reach for by name.
_TEST_CARDS: dict[str, tuple[str, str]] = {
    "pm_card_visa": ("visa", "4242"),
    "pm_card_visa_debit": ("visa", "5556"),
    "pm_card_mastercard": ("mastercard", "4444"),
    "pm_card_amex": ("amex", "8431"),
    "pm_card_discover": ("discover", "1117"),
    "pm_card_chargeDeclined": ("visa", "0002"),
    "pm_card_chargeDeclinedInsufficientFunds": ("visa", "9995"),
}
_DECLINING_CARDS = {"pm_card_chargeDeclined": "generic_decline",
                    "pm_card_chargeDeclinedInsufficientFunds": "insufficient_funds"}

_CARD_BRANDS = {"4": "visa", "5": "mastercard", "3": "amex", "6": "discover"}


def _card_details(brand: str, last4: str) -> dict:
    return {"brand": brand, "checks": None, "country": "US", "exp_month": 12,
            "exp_year": datetime.now(UTC).year + 3, "funding": "credit", "last4": last4,
            "network": brand, "three_d_secure_usage": {"supported": True}, "wallet": None}


def _payment_method(ident: Any) -> dict | None:
    """The stored payment method, minting Stripe's shared test cards on first use."""
    if not isinstance(ident, str) or not ident:
        return None
    existing = _get("payment_methods", ident)
    if existing is not None:
        return existing
    if not ident.startswith(("pm_", "card_", "tok_", "src_")):
        return None
    brand, last4 = _TEST_CARDS.get(ident, ("visa", "4242"))
    return _create("payment_methods", {"type": "card", "card": _card_details(brand, last4)},
                   ident=ident)


@app.post("/v1/payment_methods")
async def create_payment_method(request: Request):
    params = await _params(request)
    kind = params.get("type", "card")
    card = params.get("card") or {}
    number = str(card.get("number") or "4242424242424242")
    method = _create("payment_methods", {
        "type": kind,
        "card": _card_details(_CARD_BRANDS.get(number[:1], "visa"), number[-4:]),
        "billing_details": {"address": (params.get("billing_details") or {}).get("address"),
                            "email": (params.get("billing_details") or {}).get("email"),
                            "name": (params.get("billing_details") or {}).get("name"),
                            "phone": (params.get("billing_details") or {}).get("phone")},
        "customer": params.get("customer"),
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    _emit("payment_method.created", method)
    return _respond(method, params)


@app.get("/v1/payment_methods")
async def list_payment_methods(request: Request):
    params = await _params(request)
    records = _filtered(STATE["payment_methods"].values(), params, ("customer", "type"))
    return _paginate(records, params, "/v1/payment_methods")


@app.get("/v1/payment_methods/{method_id}")
async def retrieve_payment_method(method_id: str, request: Request):
    params = await _params(request)
    method = _get("payment_methods", method_id)
    if method is None:
        return _no_such("payment_method", method_id)
    return _respond(method, params)


@app.post("/v1/payment_methods/{method_id}/attach")
async def attach_payment_method(method_id: str, request: Request):
    params = await _params(request)
    customer_id = params.get("customer")
    if not customer_id:
        return _missing("customer")
    if _get("customers", customer_id) is None:
        return _no_such("customer", customer_id, param="customer")
    method = _payment_method(method_id)
    if method is None:
        return _no_such("payment_method", method_id)
    method["customer"] = customer_id
    _emit("payment_method.attached", method)
    return _respond(method, params)


@app.post("/v1/payment_methods/{method_id}/detach")
async def detach_payment_method(method_id: str, request: Request):
    params = await _params(request)
    method = _get("payment_methods", method_id)
    if method is None:
        return _no_such("payment_method", method_id)
    method["customer"] = None
    _emit("payment_method.detached", method)
    return _respond(method, params)


@app.post("/v1/payment_methods/{method_id}")
async def update_payment_method(method_id: str, request: Request):
    params = await _params(request)
    method = _get("payment_methods", method_id)
    if method is None:
        return _no_such("payment_method", method_id)
    if "billing_details" in params:
        method["billing_details"] = {**(method.get("billing_details") or {}),
                                     **(params["billing_details"] or {})}
    if "metadata" in params:
        method["metadata"] = _merge_metadata(method.get("metadata") or {}, params["metadata"])
    _emit("payment_method.updated", method)
    return _respond(method, params)


# --- payment intents and charges -----------------------------------------

def _charge_for(intent: dict, method: dict | None, *, captured: bool) -> dict:
    """The charge a confirmed PaymentIntent produces."""
    customer = _get("customers", intent.get("customer"))
    charge = _create("charges", {
        "amount": intent["amount"],
        "amount_captured": intent["amount"] if captured else 0,
        "captured": captured,
        "currency": intent["currency"],
        "customer": intent.get("customer"),
        "description": intent.get("description"),
        "invoice": intent.get("invoice"),
        "metadata": _copy(intent.get("metadata") or {}),
        "payment_intent": intent["id"],
        "payment_method": intent.get("payment_method"),
        "payment_method_details": {"card": (method or {}).get("card"), "type": "card"},
        "billing_details": {"address": None, "email": (customer or {}).get("email"),
                            "name": (customer or {}).get("name"), "phone": None},
        "status": "succeeded",
    })
    charge["receipt_url"] = f"{DASHBOARD_HOST}/receipts/{charge['id']}"
    if captured:
        txn = _balance_txn(charge["amount"], charge["currency"], "charge", charge["id"],
                           charge.get("description") or f"Charge for {charge['id']}")
        charge["balance_transaction"] = txn["id"]
    _emit("charge.succeeded", charge)
    return charge


def _confirm_intent(intent: dict, params: dict) -> Response | None:
    """Confirm a PaymentIntent, or return the card error that stopped it."""
    method = _payment_method(params.get("payment_method") or intent.get("payment_method")
                             or "pm_card_visa")
    intent["payment_method"] = method["id"] if method else None
    if method is not None and method["id"] in _DECLINING_CARDS:
        decline_code = _DECLINING_CARDS[method["id"]]
        intent["status"] = "requires_payment_method"
        intent["last_payment_error"] = {
            "code": "card_declined", "decline_code": decline_code,
            "message": "Your card was declined.", "payment_method": method["id"],
            "type": "card_error"}
        _emit("payment_intent.payment_failed", intent)
        return stripe_error(402, "Your card was declined.", type_="card_error",
                            code="card_declined", param="payment_method",
                            decline_code=decline_code)
    manual = intent.get("capture_method") == "manual"
    charge = _charge_for(intent, method, captured=not manual)
    intent["latest_charge"] = charge["id"]
    intent["last_payment_error"] = None
    if manual:
        intent["status"] = "requires_capture"
        intent["amount_capturable"] = intent["amount"]
        _emit("payment_intent.amount_capturable_updated", intent)
    else:
        intent["status"] = "succeeded"
        intent["amount_received"] = intent["amount"]
        _emit("payment_intent.succeeded", intent)
    return None


@app.post("/v1/payment_intents")
async def create_payment_intent(request: Request):
    params = await _params(request)
    if params.get("amount") is None:
        return _missing("amount")
    amount = _as_int(params["amount"])
    if amount is None:
        return _invalid_integer("amount", params["amount"])
    customer_id = params.get("customer")
    if customer_id and _get("customers", customer_id) is None:
        return _no_such("customer", customer_id, param="customer")
    method = _payment_method(params.get("payment_method"))
    intent = _create("payment_intents", {
        "amount": amount,
        "currency": params.get("currency", "usd"),
        "customer": customer_id,
        "description": params.get("description"),
        "capture_method": params.get("capture_method", "automatic"),
        "confirmation_method": params.get("confirmation_method", "automatic"),
        "payment_method": method["id"] if method else None,
        "payment_method_types": _as_list(params.get("payment_method_types")) or ["card"],
        "automatic_payment_methods": params.get("automatic_payment_methods"),
        "receipt_email": params.get("receipt_email"),
        "setup_future_usage": params.get("setup_future_usage"),
        "statement_descriptor": params.get("statement_descriptor"),
        "metadata": _merge_metadata({}, params.get("metadata")),
        "status": "requires_confirmation" if method else "requires_payment_method",
    })
    intent["client_secret"] = f"{intent['id']}_secret_checkpoint"
    _emit("payment_intent.created", intent)
    if _as_bool(params.get("confirm")):
        error = _confirm_intent(intent, params)
        if error is not None:
            return error
    return _respond(intent, params)


@app.get("/v1/payment_intents")
async def list_payment_intents(request: Request):
    params = await _params(request)
    records = _filtered(STATE["payment_intents"].values(), params, ("customer", "created"))
    return _paginate(records, params, "/v1/payment_intents")


@app.get("/v1/payment_intents/search")
async def search_payment_intents(request: Request):
    params = await _params(request)
    return _search(STATE["payment_intents"].values(), params,
                   "/v1/payment_intents/search", "payment_intents")


@app.get("/v1/payment_intents/{intent_id}")
async def retrieve_payment_intent(intent_id: str, request: Request):
    params = await _params(request)
    intent = _get("payment_intents", intent_id)
    if intent is None:
        return _no_such("payment_intent", intent_id)
    return _respond(intent, params)


@app.post("/v1/payment_intents/{intent_id}/confirm")
async def confirm_payment_intent(intent_id: str, request: Request):
    params = await _params(request)
    intent = _get("payment_intents", intent_id)
    if intent is None:
        return _no_such("payment_intent", intent_id)
    if intent["status"] in ("succeeded", "canceled"):
        return stripe_error(
            400,
            f"This PaymentIntent's status is '{intent['status']}'. It cannot be confirmed.",
            code="payment_intent_unexpected_state")
    error = _confirm_intent(intent, params)
    return error if error is not None else _respond(intent, params)


@app.post("/v1/payment_intents/{intent_id}/capture")
async def capture_payment_intent(intent_id: str, request: Request):
    params = await _params(request)
    intent = _get("payment_intents", intent_id)
    if intent is None:
        return _no_such("payment_intent", intent_id)
    if intent["status"] != "requires_capture":
        return stripe_error(
            400,
            f"This PaymentIntent could not be captured because it has a status of "
            f"'{intent['status']}'. Only a PaymentIntent with one of the following "
            f"statuses may be captured: requires_capture.",
            code="payment_intent_unexpected_state")
    amount = _as_int(params.get("amount_to_capture")) or intent["amount"]
    charge = _get("charges", intent.get("latest_charge"))
    if charge is not None:
        charge["captured"] = True
        charge["amount_captured"] = amount
        txn = _balance_txn(amount, charge["currency"], "charge", charge["id"],
                           charge.get("description") or f"Charge for {charge['id']}")
        charge["balance_transaction"] = txn["id"]
    intent["status"] = "succeeded"
    intent["amount_received"] = amount
    intent["amount_capturable"] = 0
    _emit("payment_intent.succeeded", intent)
    return _respond(intent, params)


@app.post("/v1/payment_intents/{intent_id}/cancel")
async def cancel_payment_intent(intent_id: str, request: Request):
    params = await _params(request)
    intent = _get("payment_intents", intent_id)
    if intent is None:
        return _no_such("payment_intent", intent_id)
    if intent["status"] in ("succeeded", "canceled"):
        return stripe_error(
            400,
            f"You cannot cancel this PaymentIntent because it has a status of "
            f"'{intent['status']}'.",
            code="payment_intent_unexpected_state")
    charge = _get("charges", intent.get("latest_charge"))
    if charge is not None and not charge.get("captured"):
        charge["status"] = "canceled"
        charge["paid"] = False
    intent["status"] = "canceled"
    intent["canceled_at"] = _now_unix()
    intent["cancellation_reason"] = params.get("cancellation_reason") or "requested_by_customer"
    intent["amount_capturable"] = 0
    _emit("payment_intent.canceled", intent)
    return _respond(intent, params)


@app.post("/v1/payment_intents/{intent_id}")
async def update_payment_intent(intent_id: str, request: Request):
    params = await _params(request)
    intent = _get("payment_intents", intent_id)
    if intent is None:
        return _no_such("payment_intent", intent_id)
    if "amount" in params:
        amount = _as_int(params["amount"])
        if amount is None:
            return _invalid_integer("amount", params["amount"])
        intent["amount"] = amount
    for field in ("description", "receipt_email", "statement_descriptor", "shipping",
                  "setup_future_usage", "capture_method"):
        if field in params:
            intent[field] = params[field]
    if params.get("customer"):
        if _get("customers", params["customer"]) is None:
            return _no_such("customer", params["customer"], param="customer")
        intent["customer"] = params["customer"]
    if params.get("payment_method"):
        method = _payment_method(params["payment_method"])
        intent["payment_method"] = method["id"] if method else params["payment_method"]
        if intent["status"] == "requires_payment_method":
            intent["status"] = "requires_confirmation"
    if "metadata" in params:
        intent["metadata"] = _merge_metadata(intent.get("metadata") or {}, params["metadata"])
    _emit("payment_intent.updated", intent)
    return _respond(intent, params)


@app.get("/v1/charges")
async def list_charges(request: Request):
    params = await _params(request)
    records = _filtered(STATE["charges"].values(), params,
                        ("customer", "payment_intent", "created"))
    return _paginate(records, params, "/v1/charges")


@app.get("/v1/charges/search")
async def search_charges(request: Request):
    params = await _params(request)
    return _search(STATE["charges"].values(), params, "/v1/charges/search", "charges")


@app.get("/v1/charges/{charge_id}")
async def retrieve_charge(charge_id: str, request: Request):
    params = await _params(request)
    charge = _get("charges", charge_id)
    if charge is None:
        return _no_such("charge", charge_id)
    return _respond(charge, params)


@app.post("/v1/charges/{charge_id}")
async def update_charge(charge_id: str, request: Request):
    params = await _params(request)
    charge = _get("charges", charge_id)
    if charge is None:
        return _no_such("charge", charge_id)
    for field in ("description", "receipt_email", "shipping"):
        if field in params:
            charge[field] = params[field]
    if "metadata" in params:
        charge["metadata"] = _merge_metadata(charge.get("metadata") or {}, params["metadata"])
    _emit("charge.updated", charge)
    return _respond(charge, params)


# --- refunds -------------------------------------------------------------

def _money(amount: int, currency: str) -> str:
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency, "")
    return f"{symbol}{amount / 100:,.2f}"


@app.post("/v1/refunds")
async def create_refund(request: Request):
    params = await _params(request)
    intent_id, charge_id = params.get("payment_intent"), params.get("charge")
    if not intent_id and not charge_id:
        return stripe_error(400, "One of payment_intent or charge is required.",
                            code="parameter_missing", param="payment_intent")
    intent = None
    if intent_id:
        intent = _get("payment_intents", intent_id)
        if intent is None:
            return _no_such("payment_intent", intent_id, param="payment_intent")
        charge_id = intent.get("latest_charge")
        if not charge_id:
            return stripe_error(
                400,
                f"This PaymentIntent ({intent_id}) does not have a successful charge to refund.",
                code="payment_intent_unexpected_state", param="payment_intent")
    charge = _get("charges", charge_id)
    if charge is None:
        return _no_such("charge", str(charge_id), param="charge")

    refundable = charge["amount"] - charge.get("amount_refunded", 0)
    amount = refundable if params.get("amount") is None else _as_int(params["amount"])
    if amount is None:
        return _invalid_integer("amount", params["amount"])
    if amount > refundable:
        return stripe_error(
            400,
            f"Refund amount ({_money(amount, charge['currency'])}) is greater than "
            f"unrefunded amount on charge ({_money(refundable, charge['currency'])}).",
            code="amount_too_large", param="amount")

    refund = _create("refunds", {
        "amount": amount,
        "currency": charge["currency"],
        "charge": charge["id"],
        "payment_intent": charge.get("payment_intent") or intent_id,
        "reason": params.get("reason"),
        "receipt_number": None,
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    txn = _balance_txn(-amount, charge["currency"], "refund", refund["id"],
                       f"REFUND FOR CHARGE ({charge['id']})")
    refund["balance_transaction"] = txn["id"]
    # A refund never changes the payment intent's status; the charge tracks it.
    charge["amount_refunded"] = charge.get("amount_refunded", 0) + amount
    charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
    _emit("charge.refunded", charge)
    _emit("refund.created", refund)
    return _respond(refund, params)


@app.get("/v1/refunds")
async def list_refunds(request: Request):
    params = await _params(request)
    records = _filtered(STATE["refunds"].values(), params,
                        ("payment_intent", "charge", "created"))
    return _paginate(records, params, "/v1/refunds")


@app.get("/v1/refunds/{refund_id}")
async def retrieve_refund(refund_id: str, request: Request):
    params = await _params(request)
    refund = _get("refunds", refund_id)
    if refund is None:
        return _no_such("refund", refund_id)
    return _respond(refund, params)


@app.post("/v1/refunds/{refund_id}")
async def update_refund(refund_id: str, request: Request):
    params = await _params(request)
    refund = _get("refunds", refund_id)
    if refund is None:
        return _no_such("refund", refund_id)
    if "metadata" in params:
        refund["metadata"] = _merge_metadata(refund.get("metadata") or {}, params["metadata"])
    _emit("refund.updated", refund)
    return _respond(refund, params)


# --- invoices and invoice items ------------------------------------------

def _add_interval(timestamp: int, interval: str, count: int = 1) -> int:
    """Advance a unix timestamp by a billing interval, keeping the day of month."""
    moment = datetime.fromtimestamp(timestamp, UTC)
    if interval in ("month", "year"):
        months = count * (12 if interval == "year" else 1)
        month = moment.month - 1 + months
        year, month = moment.year + month // 12, month % 12 + 1
        day = min(moment.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30,
                               31, 30, 31][month - 1])
        return int(moment.replace(year=year, month=month, day=day).timestamp())
    days = {"day": 1, "week": 7}.get(interval, 30) * count
    return timestamp + days * 86400


def _retotal(invoice: dict) -> None:
    lines = _as_list(invoice.get("lines"))
    subtotal = sum(line.get("amount", 0) for line in lines)
    invoice["subtotal"] = invoice["total"] = subtotal
    invoice["amount_due"] = subtotal - invoice.get("amount_paid", 0)
    invoice["amount_remaining"] = invoice["amount_due"]


def _attach_line(invoice: dict, item: dict) -> None:
    invoice.setdefault("lines", []).append({
        "id": _next_id("line_item"),
        "amount": item.get("amount", 0),
        "currency": item.get("currency", invoice.get("currency", "usd")),
        "description": item.get("description"),
        "invoice_item": item.get("id"),
        "price": item.get("price"),
        "quantity": item.get("quantity", 1),
        "period": item.get("period"),
        "type": "invoiceitem",
    })
    item["invoice"] = invoice["id"]
    _retotal(invoice)


def _pay_invoice(invoice: dict, *, paid_out_of_band: bool = False) -> None:
    """Settle an invoice: charge for it (unless it is free) and mark it paid."""
    amount = invoice.get("amount_due", 0)
    if amount > 0 and not paid_out_of_band:
        intent = _create("payment_intents", {
            "amount": amount, "currency": invoice.get("currency", "usd"),
            "customer": invoice.get("customer"), "description": f"Invoice {invoice['id']}",
            "invoice": invoice["id"], "status": "requires_confirmation",
            "payment_method": invoice.get("default_payment_method"),
        })
        intent["client_secret"] = f"{intent['id']}_secret_checkpoint"
        method = _payment_method(intent.get("payment_method") or "pm_card_visa")
        intent["payment_method"] = method["id"] if method else None
        charge = _charge_for(intent, method, captured=True)
        intent.update(status="succeeded", latest_charge=charge["id"], amount_received=amount)
        _emit("payment_intent.succeeded", intent)
        invoice["payment_intent"] = intent["id"]
        invoice["charge"] = charge["id"]
    invoice["status"] = "paid"
    invoice["paid"] = True
    invoice["paid_out_of_band"] = paid_out_of_band
    invoice["amount_paid"] = invoice.get("amount_paid", 0) + amount
    invoice["amount_due"] = 0
    invoice["amount_remaining"] = 0
    invoice["attempted"] = True
    invoice["attempt_count"] = invoice.get("attempt_count", 0) + 1
    invoice["status_transitions"]["paid_at"] = _now_unix()
    _emit("invoice.paid", invoice)


def _finalize_invoice(invoice: dict) -> None:
    invoice["status"] = "open"
    invoice["auto_advance"] = False
    invoice["number"] = f"{(invoice.get('customer') or 'CKPT')[-6:].upper()}-" \
                        f"{len(STATE['invoices']):04d}"
    invoice["status_transitions"]["finalized_at"] = _now_unix()
    invoice["hosted_invoice_url"] = f"{DASHBOARD_HOST}/invoices/{invoice['id']}"
    invoice["invoice_pdf"] = f"{DASHBOARD_HOST}/invoices/{invoice['id']}/pdf"
    _emit("invoice.finalized", invoice)
    if invoice.get("amount_due", 0) <= 0:
        _pay_invoice(invoice)


@app.post("/v1/invoices")
async def create_invoice(request: Request):
    params = await _params(request)
    customer_id = params.get("customer")
    if not customer_id:
        return _missing("customer")
    customer = _get("customers", customer_id)
    if customer is None or customer.get("deleted"):
        return _no_such("customer", customer_id, param="customer")
    collection_method = params.get("collection_method", "charge_automatically")
    days_until_due = _as_int(params.get("days_until_due"))
    invoice = _create("invoices", {
        "customer": customer_id,
        "customer_email": customer.get("email"),
        "customer_name": customer.get("name"),
        "currency": params.get("currency", customer.get("currency") or "usd"),
        "description": params.get("description"),
        "collection_method": collection_method,
        "auto_advance": _as_bool(params.get("auto_advance"), True),
        "days_until_due": days_until_due,
        "due_date": _now_unix() + days_until_due * 86400 if days_until_due else None,
        "default_payment_method": params.get("default_payment_method"),
        "footer": params.get("footer"),
        "subscription": params.get("subscription"),
        "metadata": _merge_metadata({}, params.get("metadata")),
        "lines": [],
    })
    # Stripe sweeps the customer's pending invoice items onto a new invoice.
    for item in STATE["invoice_items"].values():
        if item.get("customer") == customer_id and not item.get("invoice") \
                and not item.get("deleted"):
            _attach_line(invoice, item)
    _emit("invoice.created", invoice)
    return _respond(invoice, params)


@app.get("/v1/invoices")
async def list_invoices(request: Request):
    params = await _params(request)
    records = _filtered(STATE["invoices"].values(), params,
                        ("customer", "status", "subscription", "collection_method", "created"))
    return _paginate(records, params, "/v1/invoices")


@app.get("/v1/invoices/search")
async def search_invoices(request: Request):
    params = await _params(request)
    return _search(STATE["invoices"].values(), params, "/v1/invoices/search", "invoices")


@app.get("/v1/invoices/{invoice_id}")
async def retrieve_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    return _respond(invoice, params)


@app.post("/v1/invoices/{invoice_id}")
async def update_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    for field in ("description", "footer", "collection_method", "default_payment_method",
                  "due_date", "statement_descriptor"):
        if field in params:
            invoice[field] = params[field]
    if "auto_advance" in params:
        invoice["auto_advance"] = _as_bool(params["auto_advance"])
    if "metadata" in params:
        invoice["metadata"] = _merge_metadata(invoice.get("metadata") or {}, params["metadata"])
    _emit("invoice.updated", invoice)
    return _respond(invoice, params)


@app.delete("/v1/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    if invoice["status"] != "draft":
        return stripe_error(400, "You can only delete draft invoices.",
                            code="invoice_not_editable")
    invoice["deleted"] = True
    _emit("invoice.deleted", invoice)
    return JSONResponse(_deleted(invoice))


@app.post("/v1/invoices/{invoice_id}/finalize")
async def finalize_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    if invoice["status"] != "draft":
        return stripe_error(400, f"This invoice is already finalized (status "
                                 f"'{invoice['status']}').", code="invoice_not_editable")
    _finalize_invoice(invoice)
    return _respond(invoice, params)


@app.post("/v1/invoices/{invoice_id}/pay")
async def pay_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    if invoice["status"] == "paid":
        return stripe_error(400, f"Invoice {invoice_id} is already paid.",
                            code="invoice_payment_intent_requires_action")
    if invoice["status"] in ("void", "uncollectible"):
        return stripe_error(400, f"You cannot pay an invoice with status "
                                 f"'{invoice['status']}'.", code="invoice_not_editable")
    if invoice["status"] == "draft":
        _finalize_invoice(invoice)
    if invoice["status"] != "paid":
        _pay_invoice(invoice, paid_out_of_band=_as_bool(params.get("paid_out_of_band")))
    return _respond(invoice, params)


@app.post("/v1/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    # Sending a draft finalizes it first, as Stripe does.
    if invoice["status"] == "draft":
        _finalize_invoice(invoice)
    invoice["sent_at"] = _now_unix()
    _emit("invoice.sent", invoice)
    return _respond(invoice, params)


@app.post("/v1/invoices/{invoice_id}/void")
async def void_invoice(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    if invoice["status"] not in ("open", "uncollectible"):
        return stripe_error(400, f"You cannot void an invoice with status "
                                 f"'{invoice['status']}'.", code="invoice_not_editable")
    invoice["status"] = "void"
    invoice["status_transitions"]["voided_at"] = _now_unix()
    invoice["amount_due"] = 0
    invoice["amount_remaining"] = 0
    _emit("invoice.voided", invoice)
    return _respond(invoice, params)


@app.post("/v1/invoices/{invoice_id}/mark_uncollectible")
async def mark_invoice_uncollectible(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    if invoice["status"] != "open":
        return stripe_error(400, f"You cannot mark an invoice with status "
                                 f"'{invoice['status']}' uncollectible.",
                            code="invoice_not_editable")
    invoice["status"] = "uncollectible"
    invoice["status_transitions"]["marked_uncollectible_at"] = _now_unix()
    _emit("invoice.marked_uncollectible", invoice)
    return _respond(invoice, params)


@app.get("/v1/invoices/{invoice_id}/lines")
async def list_invoice_lines(invoice_id: str, request: Request):
    params = await _params(request)
    invoice = _get("invoices", invoice_id)
    if invoice is None or invoice.get("deleted"):
        return _no_such("invoice", invoice_id)
    lines = [_render_invoice_line(line) for line in _as_list(invoice.get("lines"))]
    payload = _list_obj(lines, f"/v1/invoices/{invoice_id}/lines")
    return JSONResponse(_expand(payload, params.get("expand")))


@app.post("/v1/invoiceitems")
async def create_invoice_item(request: Request):
    params = await _params(request)
    customer_id = params.get("customer")
    if not customer_id:
        return _missing("customer")
    if _get("customers", customer_id) is None:
        return _no_such("customer", customer_id, param="customer")
    quantity = _as_int(params.get("quantity")) or 1
    price = _get("prices", params.get("price"))
    if params.get("price") and price is None:
        return _no_such("price", params["price"], param="price")
    raw_amount = params.get("amount", params.get("unit_amount"))
    if raw_amount is None and price is not None:
        amount = (price.get("unit_amount") or 0) * quantity
    else:
        amount = _as_int(raw_amount if raw_amount is not None else 0)
    if amount is None:
        return _invalid_integer("amount", raw_amount)
    item = _create("invoice_items", {
        "customer": customer_id,
        "amount": amount,
        "unit_amount": _as_int(params.get("unit_amount")),
        "quantity": quantity,
        "currency": params.get("currency", (price or {}).get("currency", "usd")),
        "description": params.get("description") or (price or {}).get("nickname"),
        "price": params.get("price"),
        "period": params.get("period"),
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    item["date"] = item["created"]
    invoice_id = params.get("invoice")
    if invoice_id:
        invoice = _get("invoices", invoice_id)
        if invoice is None or invoice.get("deleted"):
            return _no_such("invoice", invoice_id, param="invoice")
        if invoice["status"] != "draft":
            return stripe_error(400, f"Invoice {invoice_id} is no longer editable because "
                                     f"it is {invoice['status']}.",
                                code="invoice_not_editable", param="invoice")
        _attach_line(invoice, item)
    _emit("invoiceitem.created", item)
    return _respond(item, params)


@app.get("/v1/invoiceitems")
async def list_invoice_items(request: Request):
    params = await _params(request)
    records = _filtered(STATE["invoice_items"].values(), params,
                        ("customer", "invoice", "created"))
    if _as_bool(params.get("pending")):
        records = [i for i in records if not i.get("invoice")]
    return _paginate(records, params, "/v1/invoiceitems")


@app.get("/v1/invoiceitems/{item_id}")
async def retrieve_invoice_item(item_id: str, request: Request):
    params = await _params(request)
    item = _get("invoice_items", item_id)
    if item is None or item.get("deleted"):
        return _no_such("invoiceitem", item_id)
    return _respond(item, params)


@app.post("/v1/invoiceitems/{item_id}")
async def update_invoice_item(item_id: str, request: Request):
    params = await _params(request)
    item = _get("invoice_items", item_id)
    if item is None or item.get("deleted"):
        return _no_such("invoiceitem", item_id)
    if "amount" in params:
        amount = _as_int(params["amount"])
        if amount is None:
            return _invalid_integer("amount", params["amount"])
        item["amount"] = amount
        invoice = _get("invoices", item.get("invoice"))
        if invoice is not None:
            for line in _as_list(invoice.get("lines")):
                if line.get("invoice_item") == item_id:
                    line["amount"] = amount
            _retotal(invoice)
    if "description" in params:
        item["description"] = params["description"]
    if "metadata" in params:
        item["metadata"] = _merge_metadata(item.get("metadata") or {}, params["metadata"])
    _emit("invoiceitem.updated", item)
    return _respond(item, params)


@app.delete("/v1/invoiceitems/{item_id}")
async def delete_invoice_item(item_id: str):
    item = _get("invoice_items", item_id)
    if item is None or item.get("deleted"):
        return _no_such("invoiceitem", item_id)
    item["deleted"] = True
    invoice = _get("invoices", item.get("invoice"))
    if invoice is not None:
        invoice["lines"] = [line for line in _as_list(invoice.get("lines"))
                            if line.get("invoice_item") != item_id]
        _retotal(invoice)
    _emit("invoiceitem.deleted", item)
    return JSONResponse(_deleted(item))


# --- subscriptions -------------------------------------------------------

def _subscription_items(raw_items: list, subscription_id: str) -> list[dict]:
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        items.append({"id": raw.get("id") or _next_id("subscription_item"),
                      "price": raw.get("price"),
                      "quantity": _as_int(raw.get("quantity")) or 1,
                      "metadata": raw.get("metadata") or {},
                      "subscription": subscription_id})
    return items


def _subscription_invoice(subscription: dict, reason: str) -> dict:
    """The invoice a subscription bills against — Stripe raises one per period."""
    customer = _get("customers", subscription.get("customer")) or {}
    invoice = _create("invoices", {
        "customer": subscription.get("customer"),
        "customer_email": customer.get("email"),
        "customer_name": customer.get("name"),
        "currency": subscription.get("currency", "usd"),
        "billing_reason": reason,
        "collection_method": subscription.get("collection_method", "charge_automatically"),
        "default_payment_method": subscription.get("default_payment_method"),
        "subscription": subscription["id"],
        "parent": {"type": "subscription_details",
                   "subscription_details": {"subscription": subscription["id"],
                                            "metadata": None}},
        "period_start": subscription.get("current_period_start"),
        "period_end": subscription.get("current_period_end"),
        "lines": [],
    })
    trialing = subscription.get("status") == "trialing"
    for item in _as_list(subscription.get("items")):
        price = _get("prices", item.get("price")) or {}
        amount = 0 if trialing else (price.get("unit_amount") or 0) * item.get("quantity", 1)
        invoice["lines"].append({
            "id": _next_id("line_item"), "amount": amount,
            "currency": price.get("currency", "usd"),
            "description": f"{item.get('quantity', 1)} x {price.get('nickname') or item.get('price')}",
            "invoice_item": None, "price": item.get("price"),
            "quantity": item.get("quantity", 1),
            "period": {"start": subscription.get("current_period_start"),
                       "end": subscription.get("current_period_end")},
            "subscription": subscription["id"], "type": "subscription",
        })
    _retotal(invoice)
    _emit("invoice.created", invoice)
    _finalize_invoice(invoice)
    if invoice["status"] == "open":
        _pay_invoice(invoice)
    subscription["latest_invoice"] = invoice["id"]
    return invoice


@app.post("/v1/subscriptions")
async def create_subscription(request: Request):
    params = await _params(request)
    customer_id = params.get("customer")
    if not customer_id:
        return _missing("customer")
    customer = _get("customers", customer_id)
    if customer is None or customer.get("deleted"):
        return _no_such("customer", customer_id, param="customer")
    raw_items = [i for i in _as_list(params.get("items")) if isinstance(i, dict)]
    if not raw_items:
        return _missing("items[0][price]")
    for index, raw in enumerate(raw_items):
        if not raw.get("price"):
            return _missing(f"items[{index}][price]")
        if _get("prices", raw["price"]) is None:
            return _no_such("price", raw["price"], param=f"items[{index}][price]")

    now = _now_unix()
    first_price = _get("prices", raw_items[0]["price"]) or {}
    recurring = first_price.get("recurring") or {"interval": "month", "interval_count": 1}
    trial_days = _as_int(params.get("trial_period_days"))
    trial_end = _as_int(params.get("trial_end")) or (
        now + trial_days * 86400 if trial_days else None)
    period_end = trial_end or _add_interval(now, recurring.get("interval", "month"),
                                            recurring.get("interval_count", 1) or 1)
    subscription = _create("subscriptions", {
        "customer": customer_id,
        "currency": first_price.get("currency", "usd"),
        "status": "trialing" if trial_end else "active",
        "items": [],
        "current_period_start": now,
        "current_period_end": period_end,
        "billing_cycle_anchor": now,
        "start_date": now,
        "cancel_at_period_end": _as_bool(params.get("cancel_at_period_end")),
        "collection_method": params.get("collection_method", "charge_automatically"),
        "default_payment_method": params.get("default_payment_method"),
        "description": params.get("description"),
        "days_until_due": _as_int(params.get("days_until_due")),
        "trial_start": now if trial_end else None,
        "trial_end": trial_end,
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    subscription["items"] = _subscription_items(raw_items, subscription["id"])
    _emit("customer.subscription.created", subscription)
    _subscription_invoice(subscription, "subscription_create")
    return _respond(subscription, params)


@app.get("/v1/subscriptions")
async def list_subscriptions(request: Request):
    params = await _params(request)
    records = _filtered(STATE["subscriptions"].values(), params,
                        ("customer", "collection_method", "created"))
    status = params.get("status")
    if status and status != "all":
        records = [s for s in records if s.get("status") == status]
    elif not status:
        # Stripe leaves canceled subscriptions out unless they are asked for.
        records = [s for s in records if s.get("status") != "canceled"]
    if params.get("price"):
        records = [s for s in records
                   if any(i.get("price") == params["price"] for i in _as_list(s.get("items")))]
    return _paginate(records, params, "/v1/subscriptions")


@app.get("/v1/subscriptions/search")
async def search_subscriptions(request: Request):
    params = await _params(request)
    return _search(STATE["subscriptions"].values(), params,
                   "/v1/subscriptions/search", "subscriptions")


@app.get("/v1/subscriptions/{subscription_id}")
async def retrieve_subscription(subscription_id: str, request: Request):
    params = await _params(request)
    subscription = _get("subscriptions", subscription_id)
    if subscription is None:
        return _no_such("subscription", subscription_id)
    return _respond(subscription, params)


@app.post("/v1/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: str, request: Request):
    params = await _params(request)
    subscription = _get("subscriptions", subscription_id)
    if subscription is None:
        return _no_such("subscription", subscription_id)
    if "cancel_at_period_end" in params:
        subscription["cancel_at_period_end"] = _as_bool(params["cancel_at_period_end"])
    for field in ("collection_method", "default_payment_method", "description",
                  "pause_collection"):
        if field in params:
            subscription[field] = params[field]
    if "trial_end" in params:
        subscription["trial_end"] = _as_int(params["trial_end"])
    if "metadata" in params:
        subscription["metadata"] = _merge_metadata(subscription.get("metadata") or {},
                                                   params["metadata"])
    raw_items = [i for i in _as_list(params.get("items")) if isinstance(i, dict)]
    if raw_items:
        existing = {i["id"]: i for i in _as_list(subscription.get("items"))}
        updated: list[dict] = []
        for raw in raw_items:
            if raw.get("price") and _get("prices", raw["price"]) is None:
                return _no_such("price", raw["price"], param="items[0][price]")
            if _as_bool(raw.get("deleted")):
                existing.pop(raw.get("id"), None)
                continue
            current = existing.pop(raw.get("id"), None) or {}
            updated.append({"id": raw.get("id") or _next_id("subscription_item"),
                            "price": raw.get("price") or current.get("price"),
                            "quantity": _as_int(raw.get("quantity"))
                            or current.get("quantity", 1),
                            "metadata": raw.get("metadata") or current.get("metadata") or {},
                            "subscription": subscription_id})
        subscription["items"] = updated + list(existing.values())
    # The MCP tool surface drives lifecycle states directly; Stripe itself has no
    # status parameter, so only known statuses are accepted.
    if params.get("status") in ("active", "past_due", "canceled", "trialing", "paused",
                                "unpaid", "incomplete"):
        subscription["status"] = params["status"]
        if params["status"] == "canceled":
            subscription["canceled_at"] = subscription["ended_at"] = _now_unix()
    _emit("customer.subscription.updated", subscription)
    return _respond(subscription, params)


@app.delete("/v1/subscriptions/{subscription_id}")
async def cancel_subscription(subscription_id: str, request: Request):
    params = await _params(request)
    subscription = _get("subscriptions", subscription_id)
    if subscription is None:
        return _no_such("subscription", subscription_id)
    subscription["status"] = "canceled"
    subscription["canceled_at"] = subscription["ended_at"] = _now_unix()
    subscription["cancel_at_period_end"] = False
    _emit("customer.subscription.deleted", subscription)
    return _respond(subscription, params)


# --- checkout sessions ---------------------------------------------------

def _session_line_items(session: dict) -> list[dict]:
    items = []
    for raw in _as_list(session.get("line_items")):
        price = _get("prices", raw.get("price"))
        quantity = _as_int(raw.get("quantity")) or 1
        amount = (price.get("unit_amount") or 0) * quantity if price else 0
        items.append({
            "id": raw.get("id"), "object": "item", "amount_discount": 0,
            "amount_subtotal": amount, "amount_tax": 0, "amount_total": amount,
            "currency": (price or {}).get("currency", session.get("currency", "usd")),
            "description": (price or {}).get("nickname") or raw.get("price"),
            "price": _render(price) if price else None, "quantity": quantity,
        })
    return items


@app.post("/v1/checkout/sessions")
async def create_checkout_session(request: Request):
    params = await _params(request)
    mode = params.get("mode", "payment")
    raw_items = [i for i in _as_list(params.get("line_items")) if isinstance(i, dict)]
    if mode != "setup" and not raw_items:
        return _missing("line_items")
    customer_id = params.get("customer")
    if customer_id and _get("customers", customer_id) is None:
        return _no_such("customer", customer_id, param="customer")
    line_items, subtotal, currency = [], 0, params.get("currency", "usd")
    for index, raw in enumerate(raw_items):
        price = _get("prices", raw.get("price"))
        if raw.get("price") and price is None:
            return _no_such("price", raw["price"], param=f"line_items[{index}][price]")
        quantity = _as_int(raw.get("quantity")) or 1
        if price is not None:
            subtotal += (price.get("unit_amount") or 0) * quantity
            currency = price.get("currency", currency)
        line_items.append({"id": _next_id("line_item"), "price": raw.get("price"),
                           "quantity": quantity})
    now = _now_unix()
    session = _create("checkout_sessions", {
        "mode": mode,
        "line_items": line_items,
        "amount_subtotal": subtotal,
        "amount_total": subtotal,
        "currency": currency,
        "customer": customer_id,
        "customer_email": params.get("customer_email"),
        "client_reference_id": params.get("client_reference_id"),
        "success_url": params.get("success_url"),
        "cancel_url": params.get("cancel_url"),
        "expires_at": now + 24 * 3600,
        "payment_method_types": _as_list(params.get("payment_method_types")) or ["card"],
        "payment_status": "no_payment_required" if mode == "setup" else "unpaid",
        "ui_mode": params.get("ui_mode", "hosted_page"),
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    session["url"] = f"{CHECKOUT_HOST}/c/pay/{session['id']}"
    _emit("checkout.session.created", session)
    return _respond(session, params)


@app.get("/v1/checkout/sessions")
async def list_checkout_sessions(request: Request):
    params = await _params(request)
    records = _filtered(STATE["checkout_sessions"].values(), params,
                        ("customer", "payment_intent", "status", "subscription", "created"))
    return _paginate(records, params, "/v1/checkout/sessions")


@app.get("/v1/checkout/sessions/{session_id}")
async def retrieve_checkout_session(session_id: str, request: Request):
    params = await _params(request)
    session = _get("checkout_sessions", session_id)
    if session is None:
        return _no_such("checkout.session", session_id)
    return _respond(session, params)


@app.get("/v1/checkout/sessions/{session_id}/line_items")
async def list_checkout_session_line_items(session_id: str, request: Request):
    params = await _params(request)
    session = _get("checkout_sessions", session_id)
    if session is None:
        return _no_such("checkout.session", session_id)
    payload = _list_obj(_session_line_items(session),
                        f"/v1/checkout/sessions/{session_id}/line_items")
    return JSONResponse(_expand(payload, params.get("expand")))


@app.post("/v1/checkout/sessions/{session_id}/expire")
async def expire_checkout_session(session_id: str, request: Request):
    params = await _params(request)
    session = _get("checkout_sessions", session_id)
    if session is None:
        return _no_such("checkout.session", session_id)
    if session["status"] != "open":
        return stripe_error(400, f"You cannot expire a Checkout Session with status "
                                 f"'{session['status']}'.")
    session["status"] = "expired"
    session["url"] = None
    _emit("checkout.session.expired", session)
    return _respond(session, params)


@app.post("/v1/checkout/sessions/{session_id}")
async def update_checkout_session(session_id: str, request: Request):
    params = await _params(request)
    session = _get("checkout_sessions", session_id)
    if session is None:
        return _no_such("checkout.session", session_id)
    if "metadata" in params:
        session["metadata"] = _merge_metadata(session.get("metadata") or {},
                                              params["metadata"])
    return _respond(session, params)


# --- events, balance -----------------------------------------------------

@app.get("/v1/events")
async def list_events(request: Request):
    params = await _params(request)
    records = _filtered(STATE["events"].values(), params, ("type", "created"))
    types = _as_list(params.get("types"))
    if types:
        patterns = [re.compile(t.replace(".", r"\.").replace("*", ".*")) for t in types]
        records = [e for e in records
                   if any(p.fullmatch(e.get("type", "")) for p in patterns)]
    return _paginate(records, params, "/v1/events")


@app.get("/v1/events/{event_id}")
async def retrieve_event(event_id: str, request: Request):
    params = await _params(request)
    event = _get("events", event_id)
    if event is None:
        return _no_such("event", event_id)
    return _respond(event, params)


@app.get("/v1/balance")
async def retrieve_balance():
    return JSONResponse(_copy(STATE["balance"]))


@app.get("/v1/balance_transactions")
async def list_balance_transactions(request: Request):
    params = await _params(request)
    records = _filtered(STATE["balance_transactions"].values(), params,
                        ("type", "currency", "source", "created"))
    return _paginate(records, params, "/v1/balance_transactions")


@app.get("/v1/balance_transactions/{txn_id}")
async def retrieve_balance_transaction(txn_id: str, request: Request):
    params = await _params(request)
    txn = _get("balance_transactions", txn_id)
    if txn is None:
        return _no_such("balance_transaction", txn_id)
    return _respond(txn, params)


# --- coupons, payment links, disputes ------------------------------------

@app.post("/v1/coupons")
async def create_coupon(request: Request):
    params = await _params(request)
    percent_off = params.get("percent_off")
    amount_off = _as_int(params.get("amount_off"))
    coupon = _create("coupons", {
        "name": params.get("name"),
        "percent_off": float(percent_off) if percent_off not in (None, "") else None,
        "amount_off": amount_off,
        "currency": params.get("currency") or ("usd" if amount_off else None),
        "duration": params.get("duration", "once"),
        "duration_in_months": _as_int(params.get("duration_in_months")),
        "max_redemptions": _as_int(params.get("max_redemptions")),
        "redeem_by": _as_int(params.get("redeem_by")),
        "metadata": _merge_metadata({}, params.get("metadata")),
    }, ident=params.get("id"))
    _emit("coupon.created", coupon)
    return _respond(coupon, params)


@app.get("/v1/coupons")
async def list_coupons(request: Request):
    params = await _params(request)
    return _paginate(_filtered(STATE["coupons"].values(), params, ("created",)),
                     params, "/v1/coupons")


@app.get("/v1/coupons/{coupon_id}")
async def retrieve_coupon(coupon_id: str, request: Request):
    params = await _params(request)
    coupon = _get("coupons", coupon_id)
    if coupon is None or coupon.get("deleted"):
        return _no_such("coupon", coupon_id)
    return _respond(coupon, params)


@app.post("/v1/coupons/{coupon_id}")
async def update_coupon(coupon_id: str, request: Request):
    params = await _params(request)
    coupon = _get("coupons", coupon_id)
    if coupon is None or coupon.get("deleted"):
        return _no_such("coupon", coupon_id)
    if "name" in params:
        coupon["name"] = params["name"]
    if "metadata" in params:
        coupon["metadata"] = _merge_metadata(coupon.get("metadata") or {}, params["metadata"])
    _emit("coupon.updated", coupon)
    return _respond(coupon, params)


@app.delete("/v1/coupons/{coupon_id}")
async def delete_coupon(coupon_id: str):
    coupon = _get("coupons", coupon_id)
    if coupon is None or coupon.get("deleted"):
        return _no_such("coupon", coupon_id)
    coupon["deleted"] = True
    coupon["valid"] = False
    _emit("coupon.deleted", coupon)
    return JSONResponse(_deleted(coupon))


@app.post("/v1/payment_links")
async def create_payment_link(request: Request):
    params = await _params(request)
    raw_items = [i for i in _as_list(params.get("line_items")) if isinstance(i, dict)]
    if not raw_items:
        return _missing("line_items")
    line_items, currency = [], "usd"
    for index, raw in enumerate(raw_items):
        price = _get("prices", raw.get("price"))
        if raw.get("price") and price is None:
            return _no_such("price", raw["price"], param=f"line_items[{index}][price]")
        if price is not None:
            currency = price.get("currency", currency)
        line_items.append({"id": _next_id("line_item"), "price": raw.get("price"),
                           "quantity": _as_int(raw.get("quantity")) or 1})
    link = _create("payment_links", {
        "line_items": line_items,
        "currency": currency,
        "allow_promotion_codes": _as_bool(params.get("allow_promotion_codes")),
        "metadata": _merge_metadata({}, params.get("metadata")),
    })
    link["url"] = f"{CHECKOUT_HOST}/b/{link['id']}"
    _emit("payment_link.created", link)
    return _respond(link, params)


@app.get("/v1/payment_links")
async def list_payment_links(request: Request):
    params = await _params(request)
    return _paginate(_filtered(STATE["payment_links"].values(), params, ("active",)),
                     params, "/v1/payment_links")


@app.get("/v1/payment_links/{link_id}")
async def retrieve_payment_link(link_id: str, request: Request):
    params = await _params(request)
    link = _get("payment_links", link_id)
    if link is None:
        return _no_such("payment_link", link_id)
    return _respond(link, params)


@app.post("/v1/payment_links/{link_id}")
async def update_payment_link(link_id: str, request: Request):
    params = await _params(request)
    link = _get("payment_links", link_id)
    if link is None:
        return _no_such("payment_link", link_id)
    if "active" in params:
        link["active"] = _as_bool(params["active"])
    if "metadata" in params:
        link["metadata"] = _merge_metadata(link.get("metadata") or {}, params["metadata"])
    _emit("payment_link.updated", link)
    return _respond(link, params)


@app.get("/v1/disputes")
async def list_disputes(request: Request):
    params = await _params(request)
    records = _filtered(STATE["disputes"].values(), params,
                        ("charge", "payment_intent", "created"))
    return _paginate(records, params, "/v1/disputes")


@app.get("/v1/disputes/{dispute_id}")
async def retrieve_dispute(dispute_id: str, request: Request):
    params = await _params(request)
    dispute = _get("disputes", dispute_id)
    if dispute is None:
        return _no_such("dispute", dispute_id)
    return _respond(dispute, params)


@app.post("/v1/disputes/{dispute_id}")
async def update_dispute(dispute_id: str, request: Request):
    params = await _params(request)
    dispute = _get("disputes", dispute_id)
    if dispute is None:
        return _no_such("dispute", dispute_id)
    evidence = params.get("evidence")
    details = dispute.setdefault("evidence_details", {"due_by": None, "has_evidence": False,
                                                      "past_due": False,
                                                      "submission_count": 0})
    if isinstance(evidence, dict):
        dispute["evidence"] = {**(dispute.get("evidence") or {}), **evidence}
        details["has_evidence"] = True
    if "metadata" in params:
        dispute["metadata"] = _merge_metadata(dispute.get("metadata") or {},
                                              params["metadata"])
    # Stripe submits the evidence unless the caller says not to.
    if _as_bool(params.get("submit"), bool(evidence)):
        dispute["status"] = "under_review"
        details["submission_count"] = details.get("submission_count", 0) + 1
    _emit("charge.dispute.updated", dispute)
    return _respond(dispute, params)


@app.post("/v1/disputes/{dispute_id}/close")
async def close_dispute(dispute_id: str, request: Request):
    params = await _params(request)
    dispute = _get("disputes", dispute_id)
    if dispute is None:
        return _no_such("dispute", dispute_id)
    dispute["status"] = "lost"
    _emit("charge.dispute.closed", dispute)
    return _respond(dispute, params)


# --- account, and the twin's MCP-only helpers ----------------------------

@app.get("/v1/account")
async def get_account_info():
    return JSONResponse(_copy(STATE["account"]))


@app.get("/v1/files")
async def list_files(request: Request):
    """Stripe's file list. The twin stores no uploads, so it is always empty."""
    params = await _params(request)
    return _paginate([], params, "/v1/files")


@app.get("/v1/search")
async def search_resources(request: Request):
    """Loose substring search across the main collections.

    Not a Stripe endpoint: it backs the ``search_stripe_resources`` MCP tool,
    whose Stripe equivalent spans resources that each have their own
    ``/search`` route.
    """
    params = await _params(request)
    needle = str(params.get("query") or "").lower()
    limit = _as_int(params.get("limit", 10)) or 10
    hits: list[dict] = []
    for collection, kind in (("customers", "customer"), ("products", "product"),
                             ("invoices", "invoice"), ("subscriptions", "subscription")):
        for record in STATE[collection].values():
            if not needle or needle in json.dumps(record).lower():
                hits.append({**_render(record), "_kind": kind})
    return JSONResponse({"object": "search_result", "url": "/v1/search",
                         "has_more": len(hits) > limit, "next_page": None,
                         "data": hits[:limit]})


@app.api_route("/v1/{rest:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def unrecognized_v1(rest: str, request: Request):
    """Anything the twin does not implement answers like Stripe's own 404."""
    return _unrecognized_url(request.method, request.url.path)


# --- MCP transport -------------------------------------------------------
# The Stripe MCP server mounts at /mcp on this same app, so its tools and the
# REST surface share one STATE dict.

from checkpoint.mcp_servers.stripe_mcp import mount_on as _mount_mcp

_mount_mcp(app)
