---
twins: [github]
seed: small-project
timeout: 60
tags: [starter, github]
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

<!--
  Every criterion above is an assertion over what your agent did, so this
  scenario runs with no API key and no model — the same as `checkpoint demo`.

  Add a judged one when you want the *answer* checked as well. It reads what
  the agent said, so it costs one model call per run and needs a judge key
  (OPENAI_API_KEY, or see `checkpoint run --help` for the alternatives):

  - [P] The final answer quotes the number of the issue it created

  [D] checks the state the agent left behind, [T] the calls it made, [P] what it
  said. "!" marks a criterion that must pass whatever the score.

  Each check runs as an assertion; `checkpoint check scenarios/quickstart.md`
  shows the exact one. Pin your own after `=>` when you want no ambiguity:

  - [D] The issue is still open
    =>  count(github.issues[title == "Add login button" && state == "open"]) == 1

  Count a guard like that rather than writing
  `github.issues[...].state == "open"`: reading a field off a selection needs
  exactly one match, so an agent that *deleted* the issue would error — "we
  could not score this run" — instead of failing.

  Note what the first criterion does *not* say: "at least one issue exists"
  would already be true of the seed, so an agent that did nothing would pass.
-->
