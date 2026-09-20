---
twins: slack
seed: incident-active
timeout: 60
tags: slack, incident
---
# Slack incident response — post the next status update

## Setup

The `incident-active` seed: the `#incident-payments-2026-05-12` channel holds
five messages describing a P1 on the Stripe webhook path — the declaration, a
diagnosis of a cert rotation, a rollback in progress, an error rate falling from
18% to 4%, and a support summary. Two of those messages already carry reactions
(`rotating_light` and `tada`); none carries `eyes`.

## Task

Post one status update in `#incident-payments-2026-05-12` summarising where the
rollback has got to. Keep it to three sentences or fewer. Then add an `eyes`
reaction to the message you just posted, so the channel can see it was
acknowledged. Do not touch the five messages that are already there.

## Criteria

- [D] Exactly 1 message was created
- [D] It was posted in the incident channel
  => created.slack.messages.channel_name == "incident-payments-2026-05-12"
- [D] It mentions the rollback  => created.slack.messages.text ~ /roll ?back|revert/i
- [D] It carries an eyes reaction  => "eyes" in created.slack.messages.reactions
- [D!] No messages were deleted
- [D!] The five seeded messages were left alone  => count(changed.slack.messages) == 0
- [D!] No channels were created or archived
  => count(created.slack.channels) == 0 && count(deleted.slack.channels) == 0
- [T] The agent made at most 12 calls
- [P] The update is three sentences or fewer and describes the current state of the rollback
