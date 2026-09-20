---
name: checkpoint
description: Test this repository's AI agent against stateful twins of the services it calls (GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace). Use whenever the user wants to evaluate, score, benchmark, gate or "test" the agent, asks what it actually did during a run, wants a scenario written, or asks whether a change is safe to ship.
---

# Checkpoint

Checkpoint runs this repository's agent — unmodified, as a subprocess — against
local twins of the SaaS APIs it calls, then checks what it *did to those
services*, not just what it said. Configuration lives in `checkpoint.toml`.

## When to use it

- The user asks to test, evaluate, score, or grade the agent.
- The user wants to know whether a change is safe to ship.
- The user asks what the agent did on a past run, or why a criterion failed.
- The user wants a new scenario written for a behaviour they care about.

## The commands

```bash
checkpoint run                      # every scenario, once each — the dev loop
checkpoint run scenarios/refund.md  # one scenario
checkpoint run --json               # machine-readable, for your own analysis
checkpoint gate                     # N runs per scenario, one SHIP/BLOCK verdict
checkpoint check                    # what each criterion will actually check
checkpoint runs show                # everything about the last run
checkpoint runs trace               # the API calls the agent made, in order
checkpoint new "<task>"             # start a scenario
checkpoint twins list               # the services available, and their seeds
```

Exit codes matter: `run` exits 1 when a criterion failed and 2 when the run
could not be scored at all. `gate` exits 0 only on SHIP (1 BLOCK, 2 CONDITIONAL,
3 INCONCLUSIVE, 4 ERROR).

## Writing scenarios

A scenario is one markdown file: front matter, `## Task`, `## Criteria`.

```markdown
---
twins: [github]
seed: small-project
---
# File a bug

## Task
File an issue in acme/webapp titled "Login broken".

## Criteria
- [D] Exactly 1 issue was created
- [D!] No issues were deleted
- [T] The agent made at most 6 calls
- [P] The final answer quotes the issue number
```

`[D]` checks state, `[T]` checks the calls, `[P]` is judged by a model. `!`
means the criterion must pass whatever the rest score. Anything after `=>` is
an explicit assertion, which makes the check deterministic and free:

```markdown
- [D] The issue is still open  =>  github.issues[title == "Login broken"].state == "open"
```

**The rule that matters:** a criterion must fail for an agent that did nothing.
"An issue exists" can already be true of the seed; "exactly one issue was
created" cannot. Run `checkpoint check` after writing one — it prints the
assertion behind each criterion, which is where a vacuous check shows itself.

## Reading a failure

1. `checkpoint runs show` — the criteria, and the reason each failed one failed.
2. `checkpoint runs trace` — every call the agent made, in order, with statuses.
3. `checkpoint run <scenario> -v` — rerun with the agent's own output streamed.

Prefer fixing the agent over loosening the criterion. If a criterion is wrong,
say so explicitly rather than quietly weakening it.
