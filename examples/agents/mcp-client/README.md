# MCP-client agent

Reference implementation of an agent that **discovers and calls tools via the
Model Context Protocol** instead of vendor REST SDKs.

Why this matters: Anthropic, OpenAI, and a growing ecosystem of agent
frameworks are standardizing on MCP for tool definitions.  Every Checkpoint
twin mounts a wire-compatible MCP server at `/mcp/` — so an agent that talks
to `github-mcp-server` in production can talk to Checkpoint's GitHub twin
unchanged.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...

checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/mcp-client \
    --docker-logs

checkpoint run scenarios/github-adversarial.md \
    --harness-dir examples/agents/mcp-client \
    --docker-logs
```

## How it works

1. The harness opens a streamable-HTTP MCP connection to
   `https://api.github.com/mcp/` (which the Checkpoint sidecar transparently
   routes to the GitHub twin's `/mcp/` endpoint at `127.0.0.1:<twin_port>`).
2. `session.list_tools()` discovers what's available.
3. The Anthropic LLM sees those tools and decides what to call.
4. Each `tool_use` block becomes a `session.call_tool(name, args)` to the MCP server.
5. Tool results round-trip back into the conversation.

The agent never imports `github`, `slack_sdk`, etc. — it only uses MCP
primitives.  Same code works against any MCP-compatible server.

## Customizing which clone

By default this hits the GitHub twin's MCP endpoint.  To point at a different
twin (Linear, Stripe, etc.), set `CHECKPOINT_MCP_URL`:

```bash
CHECKPOINT_MCP_URL=https://api.linear.app/mcp/ checkpoint run ...
```

To call **multiple** MCP servers in one run, extend `harness.py` to open
several `streamablehttp_client(...)` sessions and merge their tool lists.
