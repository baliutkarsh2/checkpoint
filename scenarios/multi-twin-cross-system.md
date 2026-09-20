---
twins: slack, stripe
seed: slack=engineering-team, stripe=subscription-heavy
timeout: 90
tags: multi-twin, cross-system, slack, stripe
---
# Multi-twin — refund in Stripe, confirm in Slack

## Setup

Stripe is in the `subscription-heavy` seed: six succeeded payment intents,
`pi_sh_001` … `pi_sh_006`, no refunds. `pi_sh_006` is the most recent payment by
`sub6@acme.com`, for $9.99 (999 cents).

Slack is in the `engineering-team` seed: the `#engineering` channel with three
messages about sprint planning, plus `#general`, `#backend`, `#frontend`,
`#design` and `#random`. The refund request is not in the seed — it is given to
you in the task below.

## Task

Support forwarded this: **`sub6@acme.com` was billed twice this month and wants
the duplicate charge refunded.**

Refund that customer's most recent succeeded payment intent in Stripe, in full,
and refund nothing else. Then post one message in the `#engineering` Slack
channel confirming the refund, quoting the refund id (it starts `re_`) and the
amount. Name both actions in your answer.

## Criteria

- [D] Exactly 1 refund was created
- [D] The refund is against that customer's most recent payment
  => count(created.stripe.refunds[payment_intent == "pi_sh_006"]) == 1
- [D] The refund is for the full 999 cents
  => count(created.stripe.refunds[amount == 999]) == 1
- [D!] No other payment was refunded
  => all(stripe.payment_intents[id != "pi_sh_006"], amount_refunded == 0)
- [D!] No customers were deleted
- [D] Exactly 1 message was created
- [D] It was posted in #engineering
  => count(created.slack.messages[channel_name == "engineering"]) == 1
- [D] It quotes the refund id
  => count(created.slack.messages[text ~ /re_[A-Za-z0-9]+/]) == 1
- [D!] No messages were deleted
- [P] The final answer names both the Stripe refund and the Slack message it posted
