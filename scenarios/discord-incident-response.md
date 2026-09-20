---
twins: discord
seed: incident-response
timeout: 120
tags: discord, incident-response, ops
---
# Discord incident response — declare and coordinate

## Setup

The `incident-response` seed: the "Ops Team" guild with `#incidents`,
`#postmortem` and `#monitoring`. `#incidents` holds one message, posted by
`monitor-bot` (message id `1330907688468480000`):

> 🚨 ALERT: Payment service latency > 5s for 3 consecutive checks. P99=8.2s.
> Started 14:32 UTC.

There is also a `monitor-bot` webhook bound to `#incidents`. Nothing is pinned
and the alert has no reactions.

## Task

You are the on-call bot. Do all of this:

1. Post an incident message in `#incidents` saying the payments service error
   rate is above 5%, and pin it.
2. Add a 🚨 reaction to the existing `monitor-bot` alert
   (message `1330907688468480000`) to mark the incident active.
3. Create a channel called `incident-2026-payments` in the guild and post
   "War room open. Join here for coordination." in it.
4. Execute the `monitor-bot` webhook to post the automated update
   "ALERT: payments error rate at 7.3%. Incident declared."

Delete nothing. Summarise what you posted where in your answer.

## Criteria

- [D] Exactly 3 messages were created
- [D] The incident message is in #incidents and reports the error rate
  => exists(created.discord.messages[channel == "incidents" && via_webhook == null && content ~ /payment/i && content ~ /5/])
- [D] That incident message is pinned
  => count(created.discord.messages[channel == "incidents" && via_webhook == null && pinned == true]) == 1
- [D] The monitor-bot alert now carries a 🚨 reaction
  => count(discord.messages[id == "1330907688468480000" && "🚨" in reactions]) == 1
- [D] Exactly 1 channel was created
- [D] A channel named "incident-2026-payments" exists
- [D] The war room channel has one message in it
  => count(created.discord.messages[channel == "incident-2026-payments"]) == 1
- [D] One message came through the monitor-bot webhook
  => count(created.discord.messages[via_webhook != null]) == 1
- [D!] No messages were deleted
- [D!] No channels were deleted
- [P] The final answer says what was posted in each channel
