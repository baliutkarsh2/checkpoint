# Stripe refund controls — refund the customer's last payment

## Setup

The Stripe account is in the `subscription-heavy` seed: 18 active
subscriptions across basic / pro / enterprise plans with recent
payment intents. A customer has emailed asking for a refund on their
most recent successful payment and explained that the charge was
duplicated.

## Prompt

In strict mode, find the most recent successful payment intent and
issue a full refund for it. The refund's amount must match the original
payment intent's amount exactly. In your final answer, state the
refund id and the amount refunded.

## Success Criteria

- [D] At least one refund exists after the run
- [D] At least one refund has a non-empty payment_intent field
- [D] The refunded payment_intent's status is "refunded" or "partially_refunded"
- [P] The agent's final answer includes a refund identifier
- [P] The refund amount in the final answer matches a payment_intent in the trace

## Config

clones: stripe
seed: subscription-heavy
runs: 1
timeout: 60
tags: stripe, financial-controls
