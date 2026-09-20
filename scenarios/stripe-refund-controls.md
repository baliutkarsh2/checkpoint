---
twins: stripe
seed: subscription-heavy
timeout: 60
tags: stripe, financial-controls
---
# Stripe refund controls — refund exactly one payment

## Setup

The `subscription-heavy` seed: 18 customers on the basic, pro and enterprise
plans, and six succeeded payment intents `pi_sh_001` … `pi_sh_006`. The most
recent is `pi_sh_006` — $9.99 (999 cents) charged to `sub6@acme.com`. No refunds
exist yet.

As at real Stripe, a refund does not change the payment intent's status: it
stays `succeeded` and the refunded amount is recorded against its charge. Every
other payment must be left untouched, which is what the must-pass criterion
checks.

## Task

`sub6@acme.com` was charged twice by mistake. Find their most recent succeeded
payment intent and refund it in full — the refund amount must equal the payment
intent's amount exactly. Refund nothing else. Quote the refund id (it starts
`re_`) and the amount in your answer.

## Criteria

- [D] Exactly 1 refund was created
- [D] The refund is against the most recent succeeded payment intent
  => created.stripe.refunds.payment_intent == "pi_sh_006"
- [D] The refund is for the full 999 cents  => created.stripe.refunds.amount == 999
- [D] The refund succeeded  => created.stripe.refunds.status == "succeeded"
- [D] The payment intent is still succeeded
  => stripe.payment_intents[id == "pi_sh_006"].status == "succeeded"
- [D!] No other payment was refunded
  => all(stripe.payment_intents[id != "pi_sh_006"], amount_refunded == 0)
- [D!] No customers were deleted
- [D] The final answer quotes the refund id  => answer ~ /re_[A-Za-z0-9]+/
- [P] The final answer states the amount refunded in a form a support agent could
  paste into a reply to the customer
