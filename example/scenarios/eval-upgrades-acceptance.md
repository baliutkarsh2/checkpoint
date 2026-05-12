# Eval upgrades acceptance (Phase 5)

## Setup

A small launch coordination workspace. GitHub seeded with `small-project`
(one repo `acme/webapp`). Stripe seeded with `small-business` (three
recent succeeded payment intents).

## Prompt

Coordinate the launch by:
1. Opening a "Launch coordination" issue on `acme/webapp`.
2. Processing a refund for one of the recent succeeded payment intents.
3. Posting a single status message in the `#engineering` Slack channel.

## Success Criteria

- [D] An issue titled "Launch coordination" exists
- [D] Exactly 1 refund exists
- [D] At least 1 channel exists
- [D] All closed issues have a comment
- [P] The agent's final answer references each of github, slack, stripe

## Config

clones: github,slack,stripe
seed: github=small-project, slack=engineering-team, stripe=small-business
runs: 1
timeout: 60
tags: demo, phase-5
