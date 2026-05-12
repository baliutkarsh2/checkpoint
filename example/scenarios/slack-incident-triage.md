# Slack incident triage

## Setup

The Slack workspace is in the `incident-active` seed: a `#incident-*` channel
already has 5 messages with reactions documenting the rollback in progress. The
agent should join the active incident channel, post a fresh status update, and
acknowledge with a reaction.

## Prompt

In the active incident channel, post a concise status update on the rollback
progress and add an `eyes` reaction so the team knows it has been seen.

## Success Criteria

- [D] At least one channel name starts with "incident-"
- [D] At least one message is posted in the incident channel
- [D] At least one message has an `eyes` reaction
- [P] The status update message references the rollback or recovery

## Config

clones: slack
seed: incident-active
runs: 1
timeout: 60
