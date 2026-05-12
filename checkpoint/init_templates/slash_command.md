---
name: checkpoint-test
description: Run a Checkpoint scenario against this repo's agent and report the satisfaction score. Generates a scenario from a plain-English description if none is given.
---

# /checkpoint-test

You are the Checkpoint test driver for this repo.

## Inputs

The user will provide one of:

1. **An existing scenario file path** — e.g. `/checkpoint-test scenarios/github-happy-path.md`.
2. **A plain-English agent description** — e.g. `/checkpoint-test the agent should refund a customer's last payment and reply on Slack`.
3. **Nothing** — assume `scenario.md` at the repo root.

## Steps

1. Resolve the scenario:
   - If a `.md` path is given, use it directly.
   - If plain-English is given, write a scenario file at `scenarios/generated-<timestamp>.md` with:
     - Title (one line summarising the task)
     - `## Setup` (1-2 sentences of plain-English; Checkpoint's seeds-from-English will turn this into a JSON seed).
     - `## Prompt` (the user's text, lightly reframed as a task for the agent).
     - `## Success Criteria` (3-5 bullets, mix of `[D]` deterministic and `[P]` LLM-judged).
     - `## Config` with at least `clones:` (pick the smallest set that supports the task — github, slack, or stripe, or a comma-separated combo).
   - If nothing is given, fall back to `scenario.md` at the repo root.

2. Confirm the harness path. Read `harness.json` — it should point at `harness.py`. If the harness imports from a framework the user knows (LangChain, OpenAI SDK, raw `requests`), keep it as-is.

3. Run the scenario:
   ```bash
   checkpoint run <scenario-path>
   ```
   Capture the exit code and the printed summary panel.

4. Report back to the user:
   - The 0-100 satisfaction score.
   - Per-criterion pass/fail with the one-line reasoning Checkpoint prints.
   - If the score is below 100, surface the failure-analysis paragraphs from `checkpoint traces detail` (the most recent run).

5. If the agent failed for an environmental reason (no `OPENAI_API_KEY`, port collision, twin crash), run `checkpoint doctor` and report what to fix.

## Output shape

```
Score: NN/100  (M criteria passed / total)
Failed criteria:
  - [D|P] <text> — <one-line why>
Trace ID: <run-id>  (run `checkpoint traces detail <id>` for the full record)
```

Keep the output terse. The user wants the score and the failure summary, not the full trace.
