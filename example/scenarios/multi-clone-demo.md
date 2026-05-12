# Multi-clone launch coordination

## Setup

A coordinated cross-team launch. GitHub is seeded with the `small-project`
seed (one repo `acme/webapp`, one existing issue). Slack is seeded with the
`engineering-team` seed (an `#engineering` channel and twelve users). Stripe
is seeded with the `small-business` seed (three recent payment intents on
plan subscriptions).

## Prompt

Coordinate the launch by:
1. Opening a "Launch coordination" issue on `acme/webapp` and adding a
   comment to it once the other steps are done.
2. Posting a status message in the `#engineering` Slack channel and adding
   an `eyes` reaction so the team knows it's been seen.
3. Processing a refund for one of the recent succeeded payment intents in
   Stripe and confirming the refund is visible.

## Success Criteria

- [D] An issue titled "Launch coordination" exists
- [P] At least one channel named "engineering" exists in the Slack state
- [P] At least one message in the Slack state has an `eyes` reaction
- [P] At least one refund exists in the Stripe state
- [P] The agent's final answer references all three of: GitHub, Slack, Stripe

## Config

clones: github,slack,stripe
seed: github=small-project, slack=engineering-team, stripe=small-business
runs: 1
timeout: 60
tags: demo, multi-clone
