"""Stripe twin driven by stripe-python, the official SDK agents reach for.

These cover the calls a billing agent actually makes — create/read/update/list a
customer, take and refund a payment, run an invoice or a subscription to the end
of its lifecycle — plus the typed errors and the pagination the SDK depends on.
"""
from __future__ import annotations

import time

import pytest

stripe = pytest.importorskip("stripe")

TWIN = "stripe"


@pytest.fixture
def sdk(twin):
    """The global-module API, pointed at the twin (how most agent code is written)."""
    stripe.api_key = twin.token
    stripe.api_base = twin.url
    stripe.max_network_retries = 0
    yield stripe
    stripe.max_network_retries = 2


@pytest.fixture
def buyer(sdk):
    return sdk.Customer.create(email="dana@example.com", name="Dana",
                               metadata={"plan": "pro"})


def paid_intent(sdk, amount=5000, **params):
    return sdk.PaymentIntent.create(amount=amount, currency="usd",
                                    payment_method="pm_card_visa", confirm=True, **params)


# --- customers -----------------------------------------------------------

def test_customer_create_retrieve_modify_delete(sdk, buyer, twin):
    assert buyer.id.startswith("cus_")
    assert buyer.metadata["plan"] == "pro"
    assert sdk.Customer.retrieve(buyer.id).email == "dana@example.com"

    renamed = sdk.Customer.modify(buyer.id, name="Dana Smith",
                                  metadata={"plan": "enterprise"})
    assert renamed.name == "Dana Smith"
    assert renamed.metadata["plan"] == "enterprise"
    assert twin.state()["customers"][buyer.id]["name"] == "Dana Smith"

    deleted = sdk.Customer.delete(buyer.id)
    assert deleted.deleted is True
    assert buyer.id not in [c.id for c in sdk.Customer.list().data]
    customers = twin.views()["customers"]
    assert customers["tombstone"] == "deleted"
    assert [c["deleted"] for c in customers["items"] if c["id"] == buyer.id] == [True]


def test_customer_list_filters_by_email(sdk, twin):
    twin.seed("small-business")
    listed = sdk.Customer.list(email="alice@bigco.com", limit=10)
    assert [c.email for c in listed.data] == ["alice@bigco.com"]


def test_customer_list_paginates_newest_first(sdk, twin):
    twin.seed("small-business")
    page = sdk.Customer.list(limit=2)
    assert page.has_more is True
    created = [c.created for c in sdk.Customer.list(limit=100).data]
    assert created == sorted(created, reverse=True)

    seen = [c.id for c in sdk.Customer.list(limit=2).auto_paging_iter()]
    assert len(seen) == len(set(seen)) == len(twin.state()["customers"])


def test_customer_search(sdk, twin):
    twin.seed("small-business")
    found = sdk.Customer.search(query="email:'carol@enterprise.com'")
    assert [c.id for c in found.data] == ["cus_003"]
    assert sdk.Customer.search(query="name~'Alice'").data[0].email == "alice@bigco.com"


def test_retrieving_an_unknown_customer_raises_invalid_request(sdk):
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.Customer.retrieve("cus_does_not_exist")
    assert exc.value.http_status == 404
    assert exc.value.code == "resource_missing"


# --- catalogue -----------------------------------------------------------

def test_product_and_price_lifecycle(sdk):
    product = sdk.Product.create(name="Pro plan", description="Everything")
    price = sdk.Price.create(product=product.id, unit_amount=2000, currency="usd",
                             recurring={"interval": "month"})
    assert price.recurring.interval == "month" and price.type == "recurring"
    assert sdk.Product.retrieve(product.id).name == "Pro plan"
    assert sdk.Price.retrieve(price.id).unit_amount == 2000
    assert [p.id for p in sdk.Price.list(product=product.id).data] == [price.id]
    assert sdk.Product.modify(product.id, active=False).active is False


def test_missing_required_param_names_the_param(sdk):
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.Price.create(product="prod_missing", currency="usd")
    assert exc.value.http_status in (400, 404)
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.Product.create()
    assert exc.value.param == "name"


# --- payments ------------------------------------------------------------

def test_payment_intent_confirmed_on_create(sdk, buyer, twin):
    intent = paid_intent(sdk, customer=buyer.id)
    assert intent.status == "succeeded"
    assert intent.latest_charge.startswith("ch_")
    assert twin.state()["charges"][intent.latest_charge]["paid"] is True
    assert sdk.PaymentIntent.retrieve(intent.id).status == "succeeded"


def test_payment_intent_manual_capture(sdk, buyer):
    intent = sdk.PaymentIntent.create(amount=3000, currency="usd", customer=buyer.id,
                                      payment_method="pm_card_visa",
                                      capture_method="manual")
    assert sdk.PaymentIntent.confirm(intent.id).status == "requires_capture"
    captured = sdk.PaymentIntent.capture(intent.id)
    assert captured.status == "succeeded" and captured.amount_received == 3000


def test_payment_intent_cancel_and_list_filter(sdk, buyer):
    intent = sdk.PaymentIntent.create(amount=100, currency="usd", customer=buyer.id)
    assert sdk.PaymentIntent.cancel(intent.id).status == "canceled"
    listed = sdk.PaymentIntent.list(customer=buyer.id)
    assert [p.id for p in listed.data] == [intent.id]


def test_payment_intent_expand_customer(sdk, buyer):
    intent = paid_intent(sdk, customer=buyer.id)
    expanded = sdk.PaymentIntent.retrieve(intent.id, expand=["customer"])
    assert expanded.customer.email == "dana@example.com"


def test_declined_card_raises_card_error(sdk, buyer):
    with pytest.raises(stripe.CardError) as exc:
        sdk.PaymentIntent.create(amount=2000, currency="usd", customer=buyer.id,
                                 payment_method="pm_card_chargeDeclined", confirm=True)
    assert exc.value.code == "card_declined"


def test_charge_list_for_customer(sdk, buyer):
    paid_intent(sdk, 1200, customer=buyer.id)
    charges = sdk.Charge.list(customer=buyer.id)
    assert [c.amount for c in charges.data] == [1200]
    assert charges.data[0].payment_method_details.card.last4 == "4242"


# --- refunds -------------------------------------------------------------

def test_partial_refund_updates_the_charge_not_the_intent(sdk, buyer, twin):
    intent = paid_intent(sdk, 5000, customer=buyer.id)
    refund = sdk.Refund.create(payment_intent=intent.id, amount=500)
    assert refund.status == "succeeded" and refund.amount == 500
    assert sdk.Refund.retrieve(refund.id).amount == 500

    charge = sdk.Charge.retrieve(intent.latest_charge)
    assert (charge.amount_refunded, charge.refunded) == (500, False)
    assert sdk.PaymentIntent.retrieve(intent.id).status == "succeeded"
    assert [r.id for r in sdk.Refund.list(payment_intent=intent.id).data] == [refund.id]


def test_refund_errors(sdk, buyer):
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.Refund.create(payment_intent="pi_does_not_exist")
    assert exc.value.code == "resource_missing"

    intent = paid_intent(sdk, 1000, customer=buyer.id)
    sdk.Refund.create(payment_intent=intent.id, amount=600)
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.Refund.create(payment_intent=intent.id, amount=600)
    assert exc.value.param == "amount"


# --- invoices ------------------------------------------------------------

def test_invoice_lifecycle(sdk, buyer, twin):
    invoice = sdk.Invoice.create(customer=buyer.id, collection_method="send_invoice",
                                 days_until_due=30)
    sdk.InvoiceItem.create(customer=buyer.id, amount=1500, currency="usd",
                           invoice=invoice.id, description="Consulting")
    finalized = sdk.Invoice.finalize_invoice(invoice.id)
    assert finalized.status == "open" and finalized.amount_due == 1500
    assert finalized.lines.data[0].description == "Consulting"

    assert sdk.Invoice.send_invoice(invoice.id).status == "open"
    paid = sdk.Invoice.pay(invoice.id)
    assert paid.status == "paid" and paid.amount_paid == 1500
    assert sdk.Invoice.retrieve(invoice.id).status == "paid"
    assert [i.id for i in sdk.Invoice.list(customer=buyer.id, status="paid").data] == [invoice.id]
    assert twin.views()["invoices"]["items"][0]["customer_email"] == "dana@example.com"


def test_draft_invoice_can_be_deleted(sdk, buyer):
    invoice = sdk.Invoice.create(customer=buyer.id)
    assert sdk.Invoice.delete(invoice.id).deleted is True
    assert [i.id for i in sdk.Invoice.list(customer=buyer.id).data] == []


# --- subscriptions -------------------------------------------------------

def test_subscription_lifecycle(sdk, twin):
    twin.seed("small-business")
    subscription = sdk.Subscription.create(
        customer="cus_001", items=[{"price": "price_002", "quantity": 1}])
    assert subscription.status == "active"
    assert subscription["items"].data[0].price.id == "price_002"

    fetched = sdk.Subscription.retrieve(subscription.id)
    assert fetched.customer == "cus_001"

    # The usual "upgrade the plan" call: swap the item's price in place.
    upgraded = sdk.Subscription.modify(
        subscription.id,
        items=[{"id": fetched["items"].data[0].id, "price": "price_003"}])
    assert [i.price.id for i in upgraded["items"].data] == ["price_003"]
    assert sdk.Subscription.modify(subscription.id,
                                   cancel_at_period_end=True).cancel_at_period_end is True
    assert [s.id for s in sdk.Subscription.list(customer="cus_001").data] == [subscription.id]

    canceled = sdk.Subscription.cancel(subscription.id)
    assert canceled.status == "canceled"
    # Canceled subscriptions drop out of the default list, as they do at Stripe.
    assert sdk.Subscription.list(customer="cus_001").data == []
    assert len(sdk.Subscription.list(customer="cus_001", status="all").data) == 1


def test_subscription_creates_an_invoice(sdk, twin):
    twin.seed("small-business")
    subscription = sdk.Subscription.create(customer="cus_002",
                                           items=[{"price": "price_001"}])
    invoice = sdk.Invoice.retrieve(subscription.latest_invoice)
    assert invoice.status == "paid" and invoice.total == 999


# --- checkout, payment methods, events, balance --------------------------

def test_checkout_session(sdk, twin):
    twin.seed("small-business")
    session = sdk.checkout.Session.create(
        mode="payment", success_url="https://acme.test/ok",
        line_items=[{"price": "price_003", "quantity": 1}])
    assert session.url.startswith("https://") and session.status == "open"
    assert sdk.checkout.Session.retrieve(session.id).amount_total == 9999
    lines = sdk.checkout.Session.list_line_items(session.id)
    assert lines.data[0].price.id == "price_003"


def test_payment_method_attach_and_list(sdk, buyer):
    method = sdk.PaymentMethod.attach("pm_card_visa", customer=buyer.id)
    assert method.customer == buyer.id and method.card.last4 == "4242"
    listed = sdk.Customer.list_payment_methods(buyer.id)
    assert [m.id for m in listed.data] == ["pm_card_visa"]
    assert sdk.PaymentMethod.detach("pm_card_visa").customer is None


def test_events_record_what_happened(sdk, buyer):
    paid_intent(sdk, 700, customer=buyer.id)
    types = [e.type for e in sdk.Event.list(limit=10).data]
    assert "payment_intent.succeeded" in types and "customer.created" in types
    assert sdk.Event.retrieve(sdk.Event.list(limit=1).data[0].id).object == "event"


def test_balance_follows_payments_and_refunds(sdk, buyer):
    before = sdk.Balance.retrieve().available[0].amount
    intent = paid_intent(sdk, 4000, customer=buyer.id)
    sdk.Refund.create(payment_intent=intent.id, amount=1000)
    assert sdk.Balance.retrieve().available[0].amount == before + 3000
    assert [t.type for t in sdk.BalanceTransaction.list(limit=2).data] == ["refund", "charge"]


# --- errors, idempotency, faults -----------------------------------------

def test_unimplemented_endpoint_is_a_typed_invalid_request(sdk):
    with pytest.raises(stripe.InvalidRequestError) as exc:
        sdk.SetupIntent.create(customer="cus_001")
    assert exc.value.http_status == 404
    assert "Unrecognized request URL" in str(exc.value)


def test_bad_api_key_raises_authentication_error(sdk, twin):
    twin.configure(strict_auth=True)
    with pytest.raises(stripe.AuthenticationError):
        sdk.Customer.list(api_key="sk_test_not_the_twins_key")


def test_idempotency_key_replays_and_conflicts(sdk):
    first = sdk.Customer.create(email="idem@example.com", idempotency_key="key-1")
    again = sdk.Customer.create(email="idem@example.com", idempotency_key="key-1")
    assert first.id == again.id

    with pytest.raises(stripe.IdempotencyError):
        sdk.Customer.create(email="other@example.com", idempotency_key="key-1")
    with pytest.raises(stripe.IdempotencyError):
        sdk.Product.create(name="Widget", idempotency_key="key-1")


def test_rate_limit_fault_raises_rate_limit_error_without_hanging(sdk, twin):
    twin.configure(rate_limit=0)
    stripe.max_network_retries = 2
    started = time.monotonic()
    try:
        with pytest.raises(stripe.RateLimitError):
            sdk.Customer.list(limit=1)
    finally:
        twin.configure(rate_limit=None)
    assert time.monotonic() - started < 30


def test_stripe_client_api_also_works(twin):
    client = stripe.StripeClient(twin.token, base_addresses={"api": twin.url},
                                 max_network_retries=0)
    twin.seed("small-business")
    customers = client.v1.customers if hasattr(client, "v1") else client.customers
    assert customers.retrieve("cus_001").email == "alice@bigco.com"


def test_trace_classifies_every_call(sdk, buyer, twin):
    intent = paid_intent(sdk, 300, customer=buyer.id)
    sdk.Refund.create(payment_intent=intent.id)
    sdk.Customer.delete(buyer.id)
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "customers") in ops
    assert ("create", "payment_intents") in ops
    assert ("create", "refunds") in ops
    assert ("delete", "customers") in ops
    assert all(entry["resource"] for entry in twin.trace())
