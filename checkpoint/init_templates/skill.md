---
name: checkpoint
description: Test this repo's agent against stateful synthetic GitHub/Slack/Stripe twins. Use whenever the user wants to evaluate, score, benchmark, or "test" an agent against realistic SaaS APIs without burning real-API credits. Use when the user asks to "run a scenario", "score the agent", "checkpoint test", or "evaluate the agent".
---

# Checkpoint — agent testing against synthetic SaaS twins

Checkpoint is installed in this repo. It runs the local agent (defined by `harness.py`) against synthetic GitHub / Slack / Stripe twins, grades the run with deterministic + LLM checks, and returns a 0-100 satisfaction score with per-criterion verdicts.

## When to invoke

- User asks to test, evaluate, score, or grade the agent.
- User wants to check what the agent does against GitHub / Slack / Stripe without hitting real APIs.
- User says "run a scenario" or "checkpoint test <something>".

## Files in this repo

- `.checkpoint.json` — default config: which clones to spin up, harness path, evaluator model, named seeds.
- `harness.py` — the agent under test. Reads `CHECKPOINT_<CLONE>_URL` env vars and `CHECKPOINT_TASK`.
- `harness.json` — manifest pointing at `harness.py` and optional prompt-file globs.
- `scenario.md` — sample scenario (Title / Setup / Prompt / Success Criteria / Config).

## How to drive Checkpoint

Run scenarios with the `checkpoint` CLI (already installed):

```bash
checkpoint run scenario.md
checkpoint run scenario.md --runs 3
checkpoint run scenario.md --tag smoke
checkpoint scenario list .
checkpoint traces detail
```

For a one-off task without a scenario file:

```bash
checkpoint run --task "Create an issue titled 'oncall' in acme/webapp"
```

For multi-clone setups, list clones in `.checkpoint.json` (`"clones": ["github","slack","stripe"]`) or in the scenario's `## Config` (`clones: github,slack,stripe`).

## Slash command

This repo also has `/checkpoint-test` (see `.claude/commands/checkpoint-test.md`). When the user says "checkpoint-test the agent on X", run that slash command — it handles scenario generation + execution + reporting end-to-end.

## Anti-patterns

- Don't ask the user to point `harness.py` at real `api.github.com` — Checkpoint already wires `CHECKPOINT_GITHUB_URL` to the local twin.
- Don't hand-write the LLM evaluator. Checkpoint's batched judge runs automatically.
- Don't run multiple `checkpoint run` invocations in parallel against the same twin port. Use `--clone` or `clone start` for long-lived sessions.
