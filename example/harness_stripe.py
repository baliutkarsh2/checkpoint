#!/usr/bin/env python3
"""Stripe acceptance harness — no LLM. Drives the Stripe twin through:
  1. list customers (sanity)
  2. list subscriptions (filter active)
  3. seed a payment_intent in state (via /_state? no — we synthesize one by
     directly inserting because strict mode forbids POST /v1/payment_intents).
     We instead use existing PIs if present; otherwise we exit with that note.
     For subscription-heavy seed there's no PI history pre-seeded — so we
     synthesize one by mutating state via a private helper if needed.
  4. refund the first available succeeded payment intent
  5. list refunds (filter by PI)

Writes a final JSON answer to stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("CHECKPOINT_STRIPE_URL") or os.environ.get("CHECKPOINT_BASE_URL") or "https://api.stripe.com"
TOKEN = os.environ.get("STRIPE_API_KEY", "sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789")
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/archal-out"))

H = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "checkpoint-stripe-acceptance/0.1",
}


def get(path, **kw):
    headers = {**H, **kw.pop("headers", {})}
    return requests.get(f"{BASE}{path}", headers=headers, timeout=15, **kw)


def post(path, **kw):
    headers = {**H, **kw.pop("headers", {})}
    return requests.post(f"{BASE}{path}", headers=headers, timeout=15, **kw)


def step(label, resp):
    print(f"[stripe] {label} -> {resp.status_code}", file=sys.stderr)
    return resp


def _seed_payment_intent_for(customer_id: str, amount: int = 2999) -> str | None:
    """Insert a succeeded payment_intent directly into twin state.

    Stripe strict mode forbids POST /v1/payment_intents, so we mutate
    via the introspection endpoints. The runner has /_state for read; we
    use a POST /_seed/<custom> trick? Not portable. Instead we issue a
    request that writes state indirectly:

    Actually, we use the local /_state contract: state is the twin's
    in-process dict. There is no /_state mutation endpoint, but we can
    fake a PI by calling the un-gated extended-mode endpoint via
    /_config strict=false first, then POST /v1/payment_intents, then
    flip strict back. The whole point of strict-vs-extended is to gate
    the *agent*; the harness is allowed to set up state.
    """
    # Flip to extended mode just to create a PI.
    requests.post(f"{BASE}/_config", json={"strict": False}, timeout=5)
    r = post("/v1/payment_intents", json={
        "amount": amount, "currency": "usd", "customer": customer_id,
    })
    pi_id = None
    if r.status_code == 200:
        pi = r.json()
        pi_id = pi["id"]
        # Mark succeeded by confirming (extended path: auto capture).
        post(f"/v1/payment_intents/{pi_id}/confirm", json={})
    # Restore strict mode.
    requests.post(f"{BASE}/_config", json={"strict": True}, timeout=5)
    return pi_id


def main():
    errors: list[str] = []

    # 1. list customers (sanity)
    r = step("list customers", get("/v1/customers?limit=50"))
    customers = r.json().get("data", []) if r.status_code == 200 else []
    if not customers:
        errors.append("no customers in seed")

    # 2. list active subscriptions
    r = step("list subscriptions", get("/v1/subscriptions?status=active&limit=50"))
    subs = r.json().get("data", []) if r.status_code == 200 else []

    # 3. find a succeeded payment_intent or synthesize one
    r = step("list PIs", get("/v1/payment_intents?limit=50"))
    pis = r.json().get("data", []) if r.status_code == 200 else []
    target_pi_id = None
    target_pi_amount = None
    for p in pis:
        if p.get("status") == "succeeded":
            target_pi_id = p["id"]
            target_pi_amount = p.get("amount", 2999)
            break
    if target_pi_id is None and customers:
        target_pi_id = _seed_payment_intent_for(customers[0]["id"], amount=2999)
        target_pi_amount = 2999

    if target_pi_id is None:
        errors.append("could not find or create a payment_intent to refund")

    # 4. refund (strict-mode allowed)
    refund_id = None
    if target_pi_id:
        r = step(
            "create refund",
            post(
                "/v1/refunds",
                headers={**H, "Idempotency-Key": f"refund-{target_pi_id}"},
                json={"payment_intent": target_pi_id, "reason": "requested_by_customer"},
            ),
        )
        if r.status_code == 200:
            refund_id = r.json()["id"]
        else:
            errors.append(f"refund failed: {r.status_code} {r.text[:120]}")

    # 5. list refunds filtered by PI
    if target_pi_id:
        r = step("list refunds", get(f"/v1/refunds?payment_intent={target_pi_id}"))
        if r.status_code != 200 or len(r.json().get("data", [])) == 0:
            errors.append(f"list refunds did not see the new refund: {r.text[:120]}")

    final = (
        f"Listed {len(subs)} active subscriptions, "
        f"refunded payment_intent {target_pi_id} (refund {refund_id})."
        if refund_id else "no refund created"
    )

    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "metrics.json").write_text(json.dumps({
            "version": 1, "llmCallCount": 0,
            "toolCallCount": 5,
            "toolErrorCount": len(errors), "exitReason": "completed",
            "provider": "none", "model": "stripe-acceptance",
        }))
        (ARCHAL_OUT / "agent-trace.json").write_text(json.dumps({
            "version": 1, "final": final,
            "events": [{"refund_id": refund_id, "payment_intent": target_pi_id}],
        }))
    except OSError:
        pass

    print(json.dumps({"text": final}))
    if errors:
        print("\n".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
