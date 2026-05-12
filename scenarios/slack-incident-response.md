# Slack incident response — post a status update

## Setup

The Slack workspace is in the `incident-active` seed: a channel whose
name starts with `incident-` already has 5 messages and reactions
documenting an in-progress rollback. The on-call engineer needs to post
the next status update.

## Prompt

In the active incident channel (channel name starts with `incident-`),
post a concise status update describing the current rollback progress
and confirm the channel acknowledged it by adding an `eyes` reaction to
the message you just posted. Keep the update under three sentences.

## Success Criteria

- [D] At least one channel name starts with "incident-"
- [D] At least one new message was posted in the incident channel during this run
- [D] At least one message in the incident channel has an `eyes` reaction
- [P] The new status update references the rollback or recovery effort

## Config

clones: slack
seed: incident-active
runs: 1
timeout: 60
tags: slack, incident
