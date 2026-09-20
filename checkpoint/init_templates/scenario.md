---
twins: github
seed: small-project
timeout: 60
tags: starter, github
---
# Starter scenario: file an issue

## Setup

The `small-project` seed: a GitHub workspace with an `acme/webapp` repository
that already has a couple of open issues. Your agent's work has to be visible
*against that starting point*, which is what the criteria below check.

## Task

Create an issue in the `acme/webapp` repository titled "Add login button" with
a body that briefly describes the request. Confirm in your final answer that
the issue was created, and quote its number.

## Criteria

- [D] Exactly 1 issue was created
- [D] An issue titled "Add login button" exists
- [D!] No issues were deleted
- [T] The agent made at most 10 calls
- [P] The final answer quotes the number of the issue it created

<!--
  [D] checks the state the agent left behind, [T] the calls it made, [P] what it
  said. "!" marks a criterion that must pass whatever the score.

  Each check runs as an assertion; `checkpoint validate scenarios/quickstart.md`
  shows the exact one. Pin your own after `=>` when you want no ambiguity:

  - [D] The issue is still open  =>  github.issues[title == "Add login button"].state == "open"

  Note what the first criterion does *not* say: "at least one issue exists"
  would already be true of the seed, so an agent that did nothing would pass.
-->
