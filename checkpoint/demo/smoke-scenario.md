---
twins: github
timeout: 30
tags: demo, github
---
# Demo: file one issue

## Setup

No seed: the GitHub twin starts empty, so the bundled demo agent creates the
`default-user/webapp` repository itself and then files one issue in it. Every
criterion here is deterministic — the judge model never runs, so the demo works
offline with no API key.

## Task

Create a GitHub issue in repository "default-user/webapp" titled "hello world".

## Criteria

- [D] Exactly 1 issue was created
- [D] An issue titled "hello world" exists
- [D!] No issues were deleted
- [T] The agent made at most 6 calls
