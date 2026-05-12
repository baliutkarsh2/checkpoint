"""Phase 3 Plan 04: Stripe extended-mode endpoints + rate-limit + seeds."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import stripe as st


SEEDS_DIR = Path(st.__file__).parent / "stripe_seeds"

EXPECTED_SEEDS = {
    "empty", "small-business", "checkout-flow",
    "subscription-heavy", "subscription-lifecycle",
}


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


def _set_extended(client):
    client.post("/_config", json={"strict": False})


# --- strict-mode 404s on extended endpoints -----------------------------

def test_retrieve_customer_404_in_strict(client):
    st.STATE["customers"]["cus_1"] = {"id": "cus_1", "object": "customer"}
    r = client.get("/v1/customers/cus_1", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "endpoint_unknown"


def test_create_payment_intent_404_in_strict(client):
    r = client.post("/v1/payment_intents", headers=H, json={"amount": 100})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "endpoint_unknown"


def test_create_subscription_404_in_strict(client):
    r = client.post("/v1/subscriptions", headers=H, json={"customer": "cus_1"})
    assert r.status_code == 404


def test_list_payment_links_404_in_strict(client):
    r = client.get("/v1/payment_links", headers=H)
    assert r.status_code == 404


# --- extended-mode happy path ------------------------------------------

def test_retrieve_customer_extended(client):
    _set_extended(client)
    st.STATE["customers"]["cus_1"] = {"id": "cus_1", "object": "customer", "email": "a@b.com"}
    r = client.get("/v1/customers/cus_1", headers=H)
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.com"


def test_retrieve_customer_not_found_extended(client):
    _set_extended(client)
    r = client.get("/v1/customers/cus_nope", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "resource_missing"


def test_create_and_confirm_payment_intent(client):
    _set_extended(client)
    r = client.post("/v1/payment_intents", headers=H, json={"amount": 1500, "currency": "usd"})
    assert r.status_code == 200
    pi = r.json()
    assert pi["status"] == "requires_confirmation"
    r2 = client.post(f"/v1/payment_intents/{pi['id']}/confirm", headers=H, json={})
    assert r2.json()["status"] == "succeeded"


def test_create_payment_intent_manual_capture(client):
    _set_extended(client)
    pi = client.post("/v1/payment_intents", headers=H,
                     json={"amount": 1000, "capture_method": "manual"}).json()
    client.post(f"/v1/payment_intents/{pi['id']}/confirm", headers=H, json={})
    assert st.STATE["payment_intents"][pi["id"]]["status"] == "requires_capture"
    r = client.post(f"/v1/payment_intents/{pi['id']}/capture", headers=H, json={})
    assert r.json()["status"] == "succeeded"


def test_cancel_payment_intent(client):
    _set_extended(client)
    pi = client.post("/v1/payment_intents", headers=H, json={"amount": 200}).json()
    r = client.post(f"/v1/payment_intents/{pi['id']}/cancel", headers=H, json={})
    assert r.json()["status"] == "canceled"


def test_handle_next_action_via_update(client):
    _set_extended(client)
    pi = client.post("/v1/payment_intents", headers=H, json={"amount": 200}).json()
    st.STATE["payment_intents"][pi["id"]]["status"] = "requires_action"
    r = client.post(f"/v1/payment_intents/{pi['id']}", headers=H, json={})
    assert r.json()["status"] == "succeeded"


def test_retrieve_refund_extended(client):
    _set_extended(client)
    st.STATE["refunds"]["re_1"] = {"id": "re_1", "object": "refund", "amount": 100}
    r = client.get("/v1/refunds/re_1", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == "re_1"


def test_pay_and_void_invoice(client):
    _set_extended(client)
    inv = client.post("/v1/invoices", headers=H, json={"customer": "cus_1"}).json()
    client.post("/v1/invoiceitems", headers=H,
                json={"customer": "cus_1", "amount": 500, "invoice": inv["id"]})
    paid = client.post(f"/v1/invoices/{inv['id']}/pay", headers=H, json={}).json()
    assert paid["status"] == "paid"
    assert paid["amount_paid"] == 500


def test_void_invoice(client):
    _set_extended(client)
    inv = client.post("/v1/invoices", headers=H, json={"customer": "cus_1"}).json()
    r = client.post(f"/v1/invoices/{inv['id']}/void", headers=H, json={})
    assert r.json()["status"] == "void"


def test_create_subscription_extended(client):
    _set_extended(client)
    r = client.post("/v1/subscriptions", headers=H, json={
        "customer": "cus_1",
        "items": [{"price": "price_test", "quantity": 1}],
    })
    assert r.status_code == 200
    sub = r.json()
    assert sub["object"] == "subscription"
    assert sub["status"] == "active"
    assert sub["customer"] == "cus_1"


def test_create_subscription_requires_customer(client):
    _set_extended(client)
    r = client.post("/v1/subscriptions", headers=H, json={})
    assert r.status_code == 400


def test_list_payment_links_extended(client):
    _set_extended(client)
    client.post("/v1/payment_links", headers=H,
                json={"line_items": [{"price": "p", "quantity": 1}]})
    r = client.get("/v1/payment_links", headers=H)
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1


# --- rate-limit ---------------------------------------------------------

def test_rate_limit_triggers_429(client):
    client.post("/_config", json={"rate_limit": 3})
    for _ in range(3):
        r = client.get("/v1/balance", headers=H)
        assert r.status_code == 200
    r = client.get("/v1/balance", headers=H)
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["message"] == "Too many requests"
    assert r.headers.get("stripe-should-retry") == "true"


def test_rate_limit_none_no_429(client):
    for _ in range(50):
        assert client.get("/v1/balance", headers=H).status_code == 200


# --- seeds --------------------------------------------------------------

def test_all_seeds_on_disk():
    found = {p.stem for p in SEEDS_DIR.glob("*.json")}
    assert EXPECTED_SEEDS.issubset(found), f"missing: {EXPECTED_SEEDS - found}"


@pytest.mark.parametrize("seed_name", sorted(EXPECTED_SEEDS))
def test_seed_loads(client, seed_name):
    r = client.post(f"/_seed/{seed_name}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["seed"] == seed_name


def test_small_business_seed_shape(client):
    client.post("/_seed/small-business")
    state = client.get("/_state").json()
    assert 5 <= len(state["customers"]) <= 10
    assert len(state["products"]) == 3
    # Has at least one succeeded payment.
    pis = state.get("payment_intents", {})
    assert any(p["status"] == "succeeded" for p in pis.values())


def test_checkout_flow_seed_has_mixed_statuses(client):
    client.post("/_seed/checkout-flow")
    state = client.get("/_state").json()
    statuses = {p["status"] for p in state["payment_intents"].values()}
    # At least 3 distinct statuses.
    assert len(statuses) >= 3


def test_subscription_heavy_seed_count(client):
    client.post("/_seed/subscription-heavy")
    state = client.get("/_state").json()
    assert 15 <= len(state["subscriptions"]) <= 20
    statuses = {s["status"] for s in state["subscriptions"].values()}
    assert "active" in statuses


def test_subscription_lifecycle_seed_has_all_stages(client):
    client.post("/_seed/subscription-lifecycle")
    state = client.get("/_state").json()
    statuses = {s["status"] for s in state["subscriptions"].values()}
    # All 4 lifecycle stages should be present.
    assert {"trialing", "active", "past_due", "canceled"}.issubset(statuses)


def test_all_seeds_parse_as_valid_json():
    for seed in EXPECTED_SEEDS:
        data = json.loads((SEEDS_DIR / f"{seed}.json").read_text())
        assert "state" in data


# --- env override of strict ---------------------------------------------

def test_env_disables_strict(monkeypatch):
    monkeypatch.setenv("STRIPE_STRICT", "false")
    # Reset state so fresh _config picks up env.
    st.STATE.clear()
    st.STATE.update(st._fresh_state())
    assert st.STATE["_config"]["strict"] is False


def test_env_default_is_strict(monkeypatch):
    monkeypatch.delenv("STRIPE_STRICT", raising=False)
    st.STATE.clear()
    st.STATE.update(st._fresh_state())
    assert st.STATE["_config"]["strict"] is True
