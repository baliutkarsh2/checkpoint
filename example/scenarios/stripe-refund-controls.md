# Stripe refund controls

## Setup

The Stripe account is in the `subscription-heavy` seed: 18 active subscriptions
across basic / pro / enterprise plans. The agent's job is to process a refund
for a recent payment intent and confirm the refund landed.

## Prompt

In strict mode, process a refund for a recent payment intent (synthesize one if
the seed has no payments yet) and confirm the refund is visible in the refunds
list filtered by that payment intent.

## Success Criteria

- [D] At least one refund exists after the run
- [D] At least one refund has a non-empty payment_intent field
- [D] The refunded payment_intent's status is "refunded" or "partially_refunded"
- [P] The agent's final answer mentions a refund identifier

## Config

clones: stripe
seed: subscription-heavy
runs: 1
timeout: 60
