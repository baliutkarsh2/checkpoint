# Example agents

Four reference harnesses you can run immediately to test Checkpoint.  Each one
demonstrates a different popular agent stack — they all use **real production
SDKs** pointed at production URLs (`https://api.github.com`,
`https://*.supabase.co`, etc.).  Checkpoint's Docker mode TLS-intercepts the
calls and routes them to the local twins.

| Agent | Stack | Best demo for |
|---|---|---|
| [`openai-tools/`](openai-tools/) | OpenAI function-calling + PyGithub + supabase-py | The most common production agent shape |
| [`anthropic-tools/`](anthropic-tools/) | Anthropic SDK tool-use + PyGithub + slack_sdk | Claude-based agents with multi-clone scenarios |
| [`langchain-react/`](langchain-react/) | LangChain ReAct + community tools | "I already use LangChain" customers |
| [`mcp-client/`](mcp-client/) | Pure MCP client hitting twin `/mcp/` endpoints | Anthropic-style MCP-first agents |

## Run any of them

Set your provider key, then:

```bash
# Pick whichever scenario + agent matches your stack
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs

checkpoint run scenarios/github-supabase-product-launch.md \
    --harness-dir examples/agents/anthropic-tools \
    --docker-logs

checkpoint run scenarios/linear-issue-triage.md \
    --harness-dir examples/agents/langchain-react

checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/mcp-client
```

`--docker-logs` streams the harness container's stderr so you can see exactly
what the agent is doing, in real time.

## How an agent integrates with Checkpoint

Every harness follows the same minimal contract:

1. **Read env vars** the runner injects:
   - `CHECKPOINT_TASK` — the prompt from the scenario
   - `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — forwarded from your shell
   - `GITHUB_TOKEN`, `SUPABASE_BOOTSTRAP_TOKEN`, etc. — pre-seeded so SDKs accept them
2. **Use real SDKs against production URLs.**  The TLS sidecar handles the routing.
3. **Print the final answer to stdout** as JSON: `{"text": "..."}`
4. **Exit 0 on success, non-zero on agent failure.**

That's it.  No checkpoint-specific imports, no test harness adapters — your
agent is the same code that runs in production.

## Building your own

The fastest path: copy the agent closest to your stack, swap out the agent
loop, keep `entrypoint.sh` and `Dockerfile` as-is.  The `entrypoint.sh`
combines the system CA bundle with the sidecar's minted CA so:

- OpenAI / Anthropic calls trust the real public CAs
- GitHub / Supabase / Slack / etc. calls trust the sidecar's per-run CA

If you skip that step, your real-API calls will fail with cert errors.

## Scenarios that work with each agent

| Agent | Best-fit scenarios |
|---|---|
| `openai-tools` | `github-happy-path.md`, `linear-issue-triage.md`, `github-supabase-product-launch.md` |
| `anthropic-tools` | `multi-clone-cross-system.md`, `slack-incident-response.md` |
| `langchain-react` | `linear-github-cross-system.md`, `linear-issue-triage.md` |
| `mcp-client` | `github-adversarial.md`, `discord-incident-response.md` |

But honestly — all four agents handle most scenarios.  Pick one and try it.
