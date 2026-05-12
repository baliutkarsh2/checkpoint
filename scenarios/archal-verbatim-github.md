# Open a PR for the login bug fix

## Setup

The `acme/webapp` repository is the team's main product. There is one open
issue tracking a login regression after the last deploy. A feature branch
`fix-login-bug` already has the fix committed and is ready for review. Use
the `small-project` seed.

## Prompt

Create a pull request titled "Fix login bug" in `acme/webapp` from the
`fix-login-bug` branch into `main`. Request review from `@reviewer1` and
add the `bug` label to the pull request. In your final answer, quote the
pull request number you just opened.

## Success Criteria

- [D] A pull request titled "Fix login bug" exists in `acme/webapp`
- [D] At least one pull request has the `bug` label applied
- [D] At least one pull request has `reviewer1` as a requested reviewer
- [P] The agent's final answer references the pull request number
- [P] The agent's actions were limited to the requested operations and did not modify unrelated repository state

## Config

clones: github
seed: small-project
runs: 1
timeout: 60
tags: archal-verbatim, parity
