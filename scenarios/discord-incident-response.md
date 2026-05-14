# Discord Incident Response

## Prompt

You are an on-call SRE bot integrated with a Discord server used by the Ops Team.
An alert has fired: the payments service error rate has exceeded 5%.

Your tasks:
1. Post an incident message to the #incidents channel describing the alert (payments service error rate > 5%).
2. Pin the incident message so it's easy to find.
3. Send a follow-up message to the #incidents channel with the initial investigation steps: "Investigating payments service. Checking pod logs and recent deployments."
4. Create a new channel called "incident-2026-payments" in the guild for the incident war room.
5. Post a message in the new war room channel: "War room opened. Please join for coordination."
6. Add a 🚨 reaction to the original incident message to signal active incident.
7. Execute the monitor-bot webhook to post an automated status update: "ALERT: payments error rate at 7.3%. Incident declared."

Use the seed state "incident-response" which provides the guild, channels, members, and webhook.

## Success Criteria

- [D] at least 1 discord message exists
- [D] at least 2 discord messages exists
- [D] exactly 1 discord channel named "incident-2026-payments" exists
- [D] at least 1 webhook exists
- [P] The incident message in #incidents describes the payments service error rate exceeding 5%
- [P] The original incident message is pinned
- [P] A war room channel was created for incident coordination
- [P] The monitor-bot webhook was used to post an automated status update
- [P] A reaction (🚨 or similar alert emoji) was added to the incident message

## Config

clones: discord
seed: discord=incident-response
timeout: 120
tags: discord, incident-response, ops
