# How to run Checkpoint

The authoritative guide is now **[README.md](./README.md)** — install, the
mental model, writing scenarios, the gate, red-teaming, simulated users,
certificates, and the full command reference. This file is a quick index.

## Quickstart

```bash
pip install checkpoint-agents          # or: pip install git+https://github.com/Aaditya2605/checkpoint
export OPENAI_API_KEY=sk-...            # for [P] LLM-judged criteria

# Run a bundled scenario against a bundled agent (Docker auto-builds the sidecar):
checkpoint run scenarios/github-happy-path.md --harness-dir examples/agents/openai-tools

# No Docker? subprocess mode (your agent reads CHECKPOINT_<CLONE>_URL):
checkpoint run scenarios/github-happy-path.md --no-docker --harness "python your_agent.py"
```

## The commands

| Command | What it does |
|---|---|
| `checkpoint init` | Scaffold zero-code integration in your repo |
| `checkpoint run <scenario>` | Run one scenario, print the score |
| `checkpoint gate <dir> --harness "..." -n 20` | Statistical release gate → SHIP/CONDITIONAL/BLOCK |
| `checkpoint redteam --harness "..."` | OWASP Agentic Top 10 adversarial pack |
| `checkpoint gen-attacks <base> --out <dir>` | Generate adversarial scenarios |
| `checkpoint redteam-mcp` | Serve a poisoned MCP server (OWASP MCP Top 10) |
| `checkpoint simulate <scenario> --harness "..."` | Multi-turn simulated user |
| `checkpoint mcp` | Run Checkpoint as an MCP server for your coding agent |
| `checkpoint cert verify <cert.json>` | Verify a signed Trust Certificate |
| `checkpoint compliance --certificate <cert.json>` | Build an Agent Assurance Report |
| `checkpoint db migrate` / `db list` | SQLite run store |
| `checkpoint otel <trace.json>` | Trajectory from an OpenTelemetry trace |
| `checkpoint serve` | Web dashboard at http://127.0.0.1:4001 |
| `checkpoint doctor` | Environment check |

Run `checkpoint <command> --help` for every option. Scenario format, twin admin
endpoints (`/_state`, `/_seed/<name>`, `/_config`, `/_trace`), and the evaluator
internals are documented in [DESIGN.md](./DESIGN.md).
