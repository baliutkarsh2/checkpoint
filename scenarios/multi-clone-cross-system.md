# Multi-clone — refund request received via Slack, executed in Stripe, replied on Slack

## Setup

Slack is in the `engineering-team` seed with an `#engineering` channel
and twelve users. Stripe is in the `subscription-heavy` seed with 18
active subscriptions and recent payment intents. A customer message has
just arrived in `#engineering` complaining about a duplicate charge and
asking for a refund on their most recent payment.

## Prompt

Read the most recent message in the `#engineering` Slack channel — it
contains a refund request. Process the refund in Stripe against the
customer's most recent successful payment intent, then reply in the
same Slack thread confirming the refund was issued. In your final
answer, summarise both actions: the Stripe refund id and the Slack
message you posted.

## Success Criteria

- [D] At least one refund exists in the Stripe state after the run
- [D] At least one refund has a non-empty payment_intent field
- [D] At least one message was posted in the Slack `#engineering` channel during this run
- [P] The Slack reply confirms the refund was issued and references a refund id
- [P] The agent's final answer references both Stripe and Slack actions

## Config

clones: slack, stripe
seed: slack=engineering-team, stripe=subscription-heavy
runs: 1
timeout: 60
tags: multi-clone, cross-system
