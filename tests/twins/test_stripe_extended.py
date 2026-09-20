"""Stripe twin: customers/payment intents/subscriptions/invoices/coupons/links, rate limits, seeds."""
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
    st.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(st.app)


TOKEN = st.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


def _customer(client, **fields) -> str:
    body = {"email": "buyer@acme.test", **fields}
    return client.post("/v1/customers", headers=H, json=body).json()["id"]


# --- full API surface --------------------------------------------------

def test_retrieve_customer(client):
    st.STATE["customers"]["cus_1"] = {"id": "cus_1", "object": "customer", "email": "a@b.com"}
    r = client.get("/v1/customers/cus_1", headers=H)
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.com"


def test_retrieve_customer_not_found(client):
    r = client.get("/v1/customers/cus_nope", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "resource_missing"


def test_create_and_confirm_payment_intent(client):
    r = client.post("/v1/payment_intents", headers=H, json={"amount": 1500, "currency": "usd"})
    assert r.status_code == 200
    pi = r.json()
    # No payment method yet, so Stripe asks for one before it can be confirmed.
    assert pi["status"] == "requires_payment_method"
    r2 = client.post(f"/v1/payment_intents/{pi['id']}/confirm", headers=H,
                     json={"payment_method": "pm_card_visa"})
    assert r2.json()["status"] == "succeeded"
    assert r2.json()["latest_charge"] in st.STATE["charges"]


def test_create_payment_intent_confirmed_inline(client):
    r = client.post("/v1/payment_intents", headers=H, json={
        "amount": 2500, "currency": "usd", "payment_method": "pm_card_visa", "confirm": True})
    assert r.json()["status"] == "succeeded"


def test_declined_test_card_is_a_card_error(client):
    r = client.post("/v1/payment_intents", headers=H, json={
        "amount": 2500, "payment_method": "pm_card_chargeDeclined", "confirm": True})
    assert r.status_code == 402
    body = r.json()
    assert (body["error"]["type"], body["error"]["code"]) == ("card_error", "card_declined")


def test_create_payment_intent_manual_capture(client):
    pi = client.post("/v1/payment_intents", headers=H,
                     json={"amount": 1000, "capture_method": "manual",
                           "payment_method": "pm_card_visa"}).json()
    client.post(f"/v1/payment_intents/{pi['id']}/confirm", headers=H, json={})
    assert st.STATE["payment_intents"][pi["id"]]["status"] == "requires_capture"
    r = client.post(f"/v1/payment_intents/{pi['id']}/capture", headers=H, json={})
    assert r.json()["status"] == "succeeded"


def test_cancel_payment_intent(client):
    pi = client.post("/v1/payment_intents", headers=H, json={"amount": 200}).json()
    r = client.post(f"/v1/payment_intents/{pi['id']}/cancel", headers=H, json={})
    assert r.json()["status"] == "canceled"


def test_cannot_cancel_a_succeeded_payment_intent(client):
    pi = client.post("/v1/payment_intents", headers=H, json={
        "amount": 200, "payment_method": "pm_card_visa", "confirm": True}).json()
    r = client.post(f"/v1/payment_intents/{pi['id']}/cancel", headers=H, json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "payment_intent_unexpected_state"


def test_confirm_advances_a_seeded_requires_action_intent(client):
    client.post("/_seed/checkout-flow")
    r = client.post("/v1/payment_intents/pi_req_action/confirm", headers=H, json={})
    assert r.json()["status"] == "succeeded"


def test_retrieve_refund(client):
    intent = client.post("/v1/payment_intents", headers=H, json={
        "amount": 100, "payment_method": "pm_card_visa", "confirm": True}).json()
    refund = client.post("/v1/refunds", headers=H,
                         json={"payment_intent": intent["id"]}).json()
    r = client.get(f"/v1/refunds/{refund['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == refund["id"]


def test_pay_and_void_invoice(client):
    buyer = _customer(client)
    inv = client.post("/v1/invoices", headers=H, json={"customer": buyer}).json()
    client.post("/v1/invoiceitems", headers=H,
                json={"customer": buyer, "amount": 500, "invoice": inv["id"]})
    paid = client.post(f"/v1/invoices/{inv['id']}/pay", headers=H, json={}).json()
    assert paid["status"] == "paid"
    assert paid["amount_paid"] == 500
    # Paying an invoice charges for it, so the charge shows up on the customer.
    assert any(c["invoice"] == inv["id"] for c in st.STATE["charges"].values())


def test_void_invoice(client):
    buyer = _customer(client)
    inv = client.post("/v1/invoices", headers=H, json={"customer": buyer}).json()
    client.post("/v1/invoiceitems", headers=H,
                json={"customer": buyer, "amount": 500, "invoice": inv["id"]})
    client.post(f"/v1/invoices/{inv['id']}/finalize", headers=H, json={})
    r = client.post(f"/v1/invoices/{inv['id']}/void", headers=H, json={})
    assert r.json()["status"] == "void"


def test_create_subscription(client):
    client.post("/_seed/small-business")
    r = client.post("/v1/subscriptions", headers=H, json={
        "customer": "cus_001",
        "items": [{"price": "price_001", "quantity": 1}],
    })
    assert r.status_code == 200
    sub = r.json()
    assert sub["object"] == "subscription"
    assert sub["status"] == "active"
    assert sub["customer"] == "cus_001"
    # Items come back as a list object of subscription items with the price inlined.
    item = sub["items"]["data"][0]
    assert item["object"] == "subscription_item"
    assert item["price"]["id"] == "price_001"
    assert sub["latest_invoice"] in st.STATE["invoices"]


def test_create_subscription_requires_customer(client):
    r = client.post("/v1/subscriptions", headers=H, json={})
    assert r.status_code == 400


def test_create_subscription_rejects_unknown_price(client):
    buyer = _customer(client)
    r = client.post("/v1/subscriptions", headers=H, json={
        "customer": buyer, "items": [{"price": "price_nope"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "resource_missing"


def test_list_payment_links(client):
    client.post("/_seed/small-business")
    client.post("/v1/payment_links", headers=H,
                json={"line_items": [{"price": "price_001", "quantity": 1}]})
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
    # stripe-python raises RateLimitError on HTTP 429 / code "rate_limit".
    assert body["error"]["code"] == "rate_limit"
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


def test_seeded_records_are_filled_out_to_full_objects(client):
    client.post("/_seed/small-business")
    customer = client.get("/v1/customers/cus_001", headers=H).json()
    assert customer["metadata"] == {} and customer["livemode"] is False
    assert customer["invoice_settings"]["default_payment_method"] is None


def test_counters_continue_past_seeded_ids(client):
    client.post("/_seed/small-business")
    assert client.post("/v1/customers", headers=H,
                       json={"email": "new@x.io"}).json()["id"] == "cus_008"


# --- pagination, filters, search, expand --------------------------------

def test_list_is_newest_first_with_cursor_pagination(client):
    client.post("/_seed/small-business")
    page = client.get("/v1/customers?limit=3", headers=H).json()
    ids = [c["id"] for c in page["data"]]
    assert ids == ["cus_007", "cus_006", "cus_005"]
    assert page["has_more"] is True
    nxt = client.get(f"/v1/customers?limit=3&starting_after={ids[-1]}", headers=H).json()
    assert [c["id"] for c in nxt["data"]] == ["cus_004", "cus_003", "cus_002"]
    back = client.get(f"/v1/customers?limit=2&ending_before={ids[-1]}", headers=H).json()
    assert [c["id"] for c in back["data"]] == ["cus_007", "cus_006"]


def test_list_filters_by_email_and_created(client):
    client.post("/_seed/small-business")
    hit = client.get("/v1/customers?email=alice@bigco.com", headers=H).json()
    assert [c["id"] for c in hit["data"]] == ["cus_001"]
    window = client.get("/v1/customers?created[gte]=1714522000", headers=H).json()
    assert len(window["data"]) == 3


def test_limit_out_of_range_is_rejected(client):
    r = client.get("/v1/customers?limit=500", headers=H)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "limit"


def test_customer_search_supports_stripe_query_syntax(client):
    client.post("/_seed/small-business")
    client.post("/v1/customers", headers=H,
                json={"email": "zed@x.io", "name": "Zed", "metadata": {"tier": "gold"}})
    exact = client.get("/v1/customers/search", headers=H,
                       params={"query": "email:'alice@bigco.com'"}).json()
    assert exact["object"] == "search_result"
    assert [c["id"] for c in exact["data"]] == ["cus_001"]
    fuzzy = client.get("/v1/customers/search", headers=H,
                       params={"query": "name~'Zed'"}).json()
    assert [c["name"] for c in fuzzy["data"]] == ["Zed"]
    tagged = client.get("/v1/customers/search", headers=H,
                        params={"query": "metadata['tier']:'gold'"}).json()
    assert len(tagged["data"]) == 1


def test_search_on_an_unknown_field_is_rejected(client):
    r = client.get("/v1/customers/search", headers=H, params={"query": "colour:'red'"})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "query"


def test_expand_inlines_a_referenced_object(client):
    client.post("/_seed/small-business")
    pi = client.get("/v1/payment_intents/pi_001", headers=H,
                    params={"expand[]": "customer"}).json()
    assert pi["customer"]["email"] == "alice@bigco.com"
    listed = client.get("/v1/payment_intents", headers=H,
                        params={"limit": 1, "expand[]": "data.customer"}).json()
    assert isinstance(listed["data"][0]["customer"], dict)


# --- deletion, checkout, events, views ----------------------------------

def test_deleted_customer_is_a_tombstone(client):
    buyer = _customer(client)
    deleted = client.delete(f"/v1/customers/{buyer}", headers=H).json()
    assert deleted == {"id": buyer, "object": "customer", "deleted": True}
    assert client.get(f"/v1/customers/{buyer}", headers=H).json()["deleted"] is True
    assert [c["id"] for c in client.get("/v1/customers", headers=H).json()["data"]] == []
    view = client.get("/_views").json()["collections"]["customers"]
    assert view["tombstone"] == "deleted"
    assert view["items"][0]["deleted"] is True


def test_checkout_session_round_trip(client):
    client.post("/_seed/small-business")
    session = client.post("/v1/checkout/sessions", headers=H, json={
        "mode": "payment", "success_url": "https://acme.test/ok",
        "line_items": [{"price": "price_002", "quantity": 2}]}).json()
    assert session["object"] == "checkout.session"
    assert session["amount_total"] == 5998
    assert session["url"].startswith("https://")
    assert "line_items" not in session
    fetched = client.get(f"/v1/checkout/sessions/{session['id']}", headers=H,
                         params={"expand[]": "line_items"}).json()
    assert fetched["line_items"]["data"][0]["quantity"] == 2
    expired = client.post(f"/v1/checkout/sessions/{session['id']}/expire", headers=H).json()
    assert expired["status"] == "expired"


def test_mutations_emit_events(client):
    buyer = _customer(client)
    client.post(f"/v1/customers/{buyer}", headers=H, json={"name": "Renamed"})
    types = [e["type"] for e in client.get("/v1/events", headers=H).json()["data"]]
    assert types[:2] == ["customer.updated", "customer.created"]


def test_views_denormalize_for_assertions(client):
    client.post("/_seed/small-business")
    client.post("/v1/subscriptions", headers=H,
                json={"customer": "cus_001", "items": [{"price": "price_002"}]})
    collections = client.get("/_views").json()["collections"]
    subscription = collections["subscriptions"]["items"][0]
    assert subscription["customer_email"] == "alice@bigco.com"
    assert subscription["plans"] == ["Pro Plan"]
    assert subscription["amount"] == 2999
    assert collections["payment_intents"]["items"][0]["customer_email"]


def test_calls_are_classified_by_resource(client):
    buyer = _customer(client)
    client.post(f"/v1/customers/{buyer}", headers=H, json={"name": "X"})
    client.delete(f"/v1/customers/{buyer}", headers=H)
    client.post("/v1/payment_intents", headers=H, json={"amount": 100})
    ops = [(e["op"], e["resource"]) for e in client.get("/_trace").json()]
    assert ops == [("create", "customers"), ("update", "customers"),
                   ("delete", "customers"), ("create", "payment_intents")]
