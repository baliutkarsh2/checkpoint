# LangChain ReAct agent

Reference implementation of an agent built with **LangChain + LangGraph's
`create_react_agent`**.  Covers GitHub (via PyGithub) and Linear (via httpx +
GraphQL).

The point: **Checkpoint is framework-agnostic.**  This harness has zero
checkpoint-specific imports.  It's a stock LangChain ReAct loop.

## Run it

```bash
export OPENAI_API_KEY=sk-...

checkpoint run scenarios/linear-issue-triage.md \
    --harness-dir examples/agents/langchain-react \
    --docker-logs

checkpoint run scenarios/linear-github-cross-system.md \
    --harness-dir examples/agents/langchain-react \
    --docker-logs
```

## Why this is interesting

LangChain's `@tool` decorator wraps any Python function as an agent-callable
tool.  Because Checkpoint runs the harness in Docker with TLS interception,
the `@tool`-wrapped functions can call the real GitHub/Linear/etc. APIs
unchanged — and Checkpoint silently routes those calls to twins.

So a customer who already has a LangChain agent in production can:

1. Drop their existing agent code into a Docker image
2. Add a thin shim that reads `CHECKPOINT_TASK` and prints the final answer
3. Get scored against any Checkpoint scenario

No rewrites, no test mocks, no changing how their agent talks to APIs.
