# `example/` — legacy harness fixtures

These files exist only as test fixtures and as the harness referenced by the
in-repo `.checkpoint.json`. They predate the curated agent set under
[`examples/agents/`](../examples/agents/).

**If you are a new user**, please look at [`examples/agents/`](../examples/agents/)
instead — those four agents (OpenAI tools, Anthropic tools, LangChain ReAct,
MCP client) are the documented, supported reference implementations. They
each ship with a Dockerfile, entrypoint, and README, and are the agents the
dashboard auto-discovers.

The contents of this directory are kept around because:
- `tests/test_phase8_performance.py` references `smoke-scenario.md`
- `.github/workflows/checkpoint-ci.yml` references `harness.py`
- The repo-root `.checkpoint.json` historically pointed at `multi_clone_harness.py`

Removing them would break CI without giving end-users any benefit.
