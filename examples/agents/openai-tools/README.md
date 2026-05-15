# OpenAI tool-calling agent

Reference implementation of an agent that uses **OpenAI's function-calling API**
to coordinate work across **GitHub**, **Supabase**, and **Slack** — all via
real production SDKs (PyGithub, supabase-py, slack_sdk).  Checkpoint's TLS
sidecar transparently routes the calls to local twins.

## What's in here

| File | Purpose |
|---|---|
| `harness.py` | The agent: 5 tools, OpenAI loop, writes metrics + trace |
| `requirements.txt` | openai, PyGithub, supabase, slack_sdk |
| `Dockerfile` | Minimal python:3.11-slim image |
| `entrypoint.sh` | Combines system CA + sidecar CA so real-API calls work |

## Run it

```bash
export OPENAI_API_KEY=sk-...

# Single-clone (GitHub-only)
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs

# Multi-clone (GitHub + Supabase)
checkpoint run scenarios/github-supabase-product-launch.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs

# CI mode (3 runs, must hit ≥80/100, machine-readable output)
checkpoint run scenarios/ \
    --harness-dir examples/agents/openai-tools \
    -n 3 --pass-threshold 80 -o json -q
```

## How the agent decides what to do

1. The runner injects `CHECKPOINT_TASK` (the prompt from the scenario).
2. The agent kicks off an OpenAI chat-completion loop with the 5 tools above.
3. On each turn, it either calls one or more tools or emits a final text answer.
4. Tool results round-trip back into the conversation as `role: "tool"` messages.
5. After the final answer, the harness writes `/archal-out/metrics.json` and
   `/archal-out/agent-trace.json` so Checkpoint records token / call counts.

Max steps default to 12 (set `CHECKPOINT_MAX_STEPS`).

## Tool surface

| Tool | What it does |
|---|---|
| `github_create_issue` | POST `/repos/{owner}/{repo}/issues` |
| `github_list_issues` | GET `/repos/{owner}/{repo}/issues` |
| `supabase_select` | SELECT from a Supabase table with optional `.eq()` filter |
| `supabase_insert` | INSERT a row (column values nested under `record`) |
| `slack_post_message` | `chat.postMessage` to a channel |

Add your own by following the existing pattern: define the function, register
it in `DISPATCH`, add a JSON-schema entry in `TOOLS`.

## Customizing the model

```bash
# Use GPT-4o instead of mini
CHECKPOINT_AGENT_MODEL=gpt-4o checkpoint run ...

# Different env var name? Read it in harness.py — Checkpoint just forwards env.
```
