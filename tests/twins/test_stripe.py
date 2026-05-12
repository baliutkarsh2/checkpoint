"""Phase 3 Plan 03: Stripe twin strict-mode endpoints + auth + idempotency."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import stripe as st


@pytest.fixture(autouse=True)
def _reset_state():
    st.STATE.clear()
    st.STATE.update(st._fresh_state())
    st.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(st.app)


TOKEN = st.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


# --- auth gate ----------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/v1/balance")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "did not provide" in body["error"]["message"].lower()


def test_wrong_token_returns_401(client):
    r = client.get("/v1/balance", headers={"Authorization": "Bearer sk_test_wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_env_override_bootstrap_token(monkeypatch, client):
    monkeypatch.setenv("STRIPE_BOOTSTRAP_TOKEN", "sk_live_override")
    r = client.get("/v1/balance", headers=H)
    assert r.status_code == 401
    r = client.get("/v1/balance", headers={"Authorization": "Bearer sk_live_override"})
    assert r.status_code == 200


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


# --- customers ----------------------------------------------------------

def test_create_customer_json(client):
    r = client.post("/v1/customers", headers=H, json={"email": "a@b.com", "name": "Alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "customer"
    assert body["email"] == "a@b.com"
    assert body["id"].startswith("cus_")


def test_create_customer_form_encoded(client):
    r = client.post(
        "/v1/customers",
        headers={**H, "Content-Type": "application/x-www-form-urlencoded"},
        content="email=form%40b.com&name=Bob",
    )
    assert r.status_code == 200
    assert r.json()["email"] == "form@b.com"


def test_list_customers(client):
    client.post("/v1/customers", headers=H, json={"email": "a@b.com"})
    client.post("/v1/customers", headers=H, json={"email": "c@d.com"})
    r = client.get("/v1/customers?limit=10", headers=H)
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 2


# --- products / prices --------------------------------------------------

def test_create_product_requires_name(client):
    r = client.post("/v1/products", headers=H, json={})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "name"


def test_create_product_and_price(client):
    r = client.post("/v1/products", headers=H, json={"name": "Pro Plan"})
    assert r.status_code == 200
    prod = r.json()
    r = client.post("/v1/prices", headers=H, json={
        "product": prod["id"], "unit_amount": 1999, "currency": "usd"
    })
    assert r.status_code == 200
    price = r.json()
    assert price["product"] == prod["id"]
    assert price["unit_amount"] == 1999


def test_create_price_missing_product(client):
    r = client.post("/v1/prices", headers=H, json={"unit_amount": 100})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "product"


def test_list_prices_filter_by_product(client):
    p1 = client.post("/v1/products", headers=H, json={"name": "A"}).json()
    p2 = client.post("/v1/products", headers=H, json={"name": "B"}).json()
    client.post("/v1/prices", headers=H, json={"product": p1["id"], "unit_amount": 100})
    client.post("/v1/prices", headers=H, json={"product": p2["id"], "unit_amount": 200})
    r = client.get(f"/v1/prices?product={p1['id']}", headers=H)
    body = r.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["product"] == p1["id"]


# --- payment_intents (list only in strict) ------------------------------

def test_list_payment_intents_empty(client):
    r = client.get("/v1/payment_intents", headers=H)
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_list_payment_intents_filter_by_customer(client):
    st.STATE["payment_intents"]["pi_1"] = {"id": "pi_1", "customer": "cus_a", "amount": 100, "created": 1}
    st.STATE["payment_intents"]["pi_2"] = {"id": "pi_2", "customer": "cus_b", "amount": 200, "created": 2}
    r = client.get("/v1/payment_intents?customer=cus_a", headers=H)
    body = r.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "pi_1"


# --- refunds ------------------------------------------------------------

def test_create_refund_for_payment_intent_marks_it_refunded(client):
    st.STATE["payment_intents"]["pi_1"] = {
        "id": "pi_1", "customer": "cus_a", "amount": 500, "status": "succeeded", "created": 1,
    }
    r = client.post("/v1/refunds", headers=H, json={"payment_intent": "pi_1"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "refund"
    assert body["payment_intent"] == "pi_1"
    assert body["amount"] == 500
    assert st.STATE["payment_intents"]["pi_1"]["status"] == "refunded"


def test_partial_refund_marks_partially_refunded(client):
    st.STATE["payment_intents"]["pi_2"] = {
        "id": "pi_2", "customer": "cus_a", "amount": 1000, "status": "succeeded", "created": 1,
    }
    r = client.post("/v1/refunds", headers=H, json={"payment_intent": "pi_2", "amount": 200})
    assert r.status_code == 200
    assert st.STATE["payment_intents"]["pi_2"]["status"] == "partially_refunded"


def test_refund_requires_pi_or_charge(client):
    r = client.post("/v1/refunds", headers=H, json={})
    assert r.status_code == 400


def test_list_refunds_filter(client):
    client.post("/v1/refunds", headers=H, json={"charge": "ch_1", "amount": 100})
    client.post("/v1/refunds", headers=H, json={"payment_intent": "pi_x", "amount": 50})
    r = client.get("/v1/refunds?payment_intent=pi_x", headers=H)
    assert len(r.json()["data"]) == 1


# --- invoices -----------------------------------------------------------

def test_invoice_lifecycle(client):
    inv = client.post("/v1/invoices", headers=H, json={"customer": "cus_1"}).json()
    assert inv["status"] == "draft"
    item = client.post(
        "/v1/invoiceitems", headers=H,
        json={"customer": "cus_1", "amount": 1500, "invoice": inv["id"]},
    ).json()
    assert item["amount"] == 1500
    finalized = client.post(f"/v1/invoices/{inv['id']}/finalize", headers=H).json()
    assert finalized["status"] == "open"
    assert finalized["amount_due"] == 1500


def test_invoice_requires_customer(client):
    r = client.post("/v1/invoices", headers=H, json={})
    assert r.status_code == 400


def test_finalize_unknown_invoice_404(client):
    r = client.post("/v1/invoices/in_nope/finalize", headers=H)
    assert r.status_code == 404


# --- subscriptions ------------------------------------------------------

def test_subscription_update_and_cancel(client):
    st.STATE["subscriptions"]["sub_1"] = {
        "id": "sub_1", "object": "subscription", "customer": "cus_1",
        "status": "active", "current_period_end": 9999999,
    }
    r = client.post("/v1/subscriptions/sub_1", headers=H, json={"cancel_at_period_end": True})
    assert r.status_code == 200
    assert r.json()["cancel_at_period_end"] is True

    r2 = client.delete("/v1/subscriptions/sub_1", headers=H)
    assert r2.status_code == 200
    assert r2.json()["status"] == "canceled"


def test_subscription_list_filter_status(client):
    st.STATE["subscriptions"]["sub_a"] = {"id": "sub_a", "customer": "c1", "status": "active"}
    st.STATE["subscriptions"]["sub_b"] = {"id": "sub_b", "customer": "c1", "status": "past_due"}
    r = client.get("/v1/subscriptions?status=active", headers=H)
    assert len(r.json()["data"]) == 1


def test_cancel_unknown_subscription_404(client):
    r = client.delete("/v1/subscriptions/sub_nope", headers=H)
    assert r.status_code == 404


# --- balance / coupons / payment_links / disputes / search --------------

def test_retrieve_balance(client):
    r = client.get("/v1/balance", headers=H)
    body = r.json()
    assert body["object"] == "balance"
    assert isinstance(body["available"], list)


def test_create_and_list_coupon(client):
    r = client.post("/v1/coupons", headers=H, json={"percent_off": 25, "duration": "once"})
    assert r.status_code == 200
    assert r.json()["object"] == "coupon"
    r = client.get("/v1/coupons", headers=H)
    assert len(r.json()["data"]) == 1


def test_create_payment_link_requires_line_items(client):
    r = client.post("/v1/payment_links", headers=H, json={})
    assert r.status_code == 400


def test_create_payment_link_happy(client):
    r = client.post("/v1/payment_links", headers=H, json={"line_items": [{"price": "p", "quantity": 1}]})
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://")


def test_disputes_list_empty(client):
    r = client.get("/v1/disputes", headers=H)
    assert r.json()["data"] == []


def test_dispute_update_submit(client):
    st.STATE["disputes"]["du_1"] = {"id": "du_1", "object": "dispute", "status": "warning_needs_response"}
    r = client.post("/v1/disputes/du_1", headers=H, json={"submit": True})
    assert r.json()["status"] == "under_review"


def test_search_resources(client):
    client.post("/v1/customers", headers=H, json={"email": "needle@x.com"})
    client.post("/v1/customers", headers=H, json={"email": "haystack@x.com"})
    r = client.get("/v1/search?query=needle", headers=H)
    body = r.json()
    assert body["object"] == "search_result"
    assert len(body["data"]) == 1
    assert body["data"][0]["email"] == "needle@x.com"


def test_fetch_resources_returns_empty_list(client):
    r = client.get("/v1/files", headers=H)
    assert r.json()["data"] == []


def test_account_info(client):
    r = client.get("/v1/account", headers=H)
    body = r.json()
    assert body["object"] == "account"
    assert body["country"] == "US"


# --- idempotency --------------------------------------------------------

def test_idempotency_key_same_returns_cached(client):
    r1 = client.post(
        "/v1/customers", headers={**H, "Idempotency-Key": "k1"},
        json={"email": "x@y.com"},
    )
    r2 = client.post(
        "/v1/customers", headers={**H, "Idempotency-Key": "k1"},
        json={"email": "x@y.com"},
    )
    assert r1.json()["id"] == r2.json()["id"]
    # State only contains one customer.
    assert len(st.STATE["customers"]) == 1


def test_idempotency_key_different_creates_two(client):
    r1 = client.post("/v1/customers", headers={**H, "Idempotency-Key": "k1"}, json={"email": "a@y.com"})
    r2 = client.post("/v1/customers", headers={**H, "Idempotency-Key": "k2"}, json={"email": "b@y.com"})
    assert r1.json()["id"] != r2.json()["id"]
    assert len(st.STATE["customers"]) == 2


def test_idempotency_no_key_always_creates(client):
    client.post("/v1/customers", headers=H, json={"email": "a@y.com"})
    client.post("/v1/customers", headers=H, json={"email": "a@y.com"})
    assert len(st.STATE["customers"]) == 2


def test_idempotency_on_refunds(client):
    st.STATE["payment_intents"]["pi_5"] = {
        "id": "pi_5", "customer": "c1", "amount": 100, "status": "succeeded",
    }
    r1 = client.post("/v1/refunds", headers={**H, "Idempotency-Key": "rk"},
                     json={"payment_intent": "pi_5"})
    r2 = client.post("/v1/refunds", headers={**H, "Idempotency-Key": "rk"},
                     json={"payment_intent": "pi_5"})
    assert r1.json()["id"] == r2.json()["id"]
    assert len(st.STATE["refunds"]) == 1
