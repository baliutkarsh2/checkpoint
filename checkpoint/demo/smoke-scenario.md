---
twins: [github]
seed: small-project
timeout: 60
tags: [demo]
---
# File an issue

## Setup

A GitHub workspace that already contains the `acme/webapp` repository and a
couple of open issues. Every criterion below is checked against that starting
point, so an agent that does nothing cannot pass.

## Task

Create an issue in the `acme/webapp` repository titled "Login button is missing".

## Criteria

- [D] Exactly 1 issue was created
- [D!] No issues were deleted
- [T] The agent made at most 10 calls
