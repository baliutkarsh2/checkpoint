# Anthropic Claude tool-use agent

Reference implementation of an agent that uses **Claude's tool-use protocol**
(`tool_use` blocks + `tool_result` blocks) to coordinate work across **GitHub**,
**Supabase**, and **Slack**.  Same real-SDK pattern as `openai-tools/` —
PyGithub, supabase-py, slack_sdk against production URLs.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Multi-clone — Claude is great at the cross-system reasoning these need
checkpoint run scenarios/multi-clone-cross-system.md \
    --harness-dir examples/agents/anthropic-tools \
    --docker-logs

# Slack incident response
checkpoint run scenarios/slack-incident-response.md \
    --harness-dir examples/agents/anthropic-tools \
    --docker-logs
```

## What's different from the OpenAI version

The agent loop differs in protocol shape:

| OpenAI | Anthropic |
|---|---|
| `messages.create(... tools=[...])` returns `tool_calls[]` | `messages.create(...)` returns `content` blocks of type `tool_use` |
| Each tool result is its own `role: "tool"` message with `tool_call_id` | All tool results in one turn batch into a single `role: "user"` message of `tool_result` blocks |
| Stop on `finish_reason != "tool_calls"` | Stop on `stop_reason != "tool_use"` |

The tool implementations themselves (`t_github_create_issue`, etc.) are
identical to the OpenAI version — they call the same SDKs against the same
production URLs.

## Customizing the model

```bash
CHECKPOINT_AGENT_MODEL=claude-opus-4-7 checkpoint run ...
```

Defaults to `claude-sonnet-4-6`.
