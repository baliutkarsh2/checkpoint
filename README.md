# checkpoint

Minimum-viable clone of Archal. Stateful SaaS twins + scenario runner + LLM judge.

## What works (v0)

- `checkpoint run scenario.md --harness "python harness.py"` end-to-end
- One twin: GitHub (issues, comments, labels, repos — REST subset)
- Scenario format: drop-in compatible with Archal's markdown spec (Title, Setup, Prompt, Success Criteria with `[D]` / `[P]`, Config)
- `[D]` deterministic checks via regex + state inspection (small built-in pattern set; unhandled criteria fall through to LLM judge)
- `[P]` LLM judge via OpenAI (batched per run, `gpt-4o-mini` default)
- Multi-run satisfaction (`runs:` config or `--runs N`)
- Trace dump (`--trace-out trace.json`)

## What's deliberately missing vs Archal

- **No TLS proxy.** Harness reads `CHECKPOINT_GITHUB_URL` env var and points its HTTP client at the twin directly. Adding mitmproxy interception is the obvious v1 upgrade (~1-2 days).
- **No Docker mode.** Harness runs as a plain subprocess.
- **No seeds-from-English.** Twin starts empty. Set up via tool calls inside the scenario prompt or pre-seed via direct HTTP.
- **One clone per run.** No multi-service scenarios.
- **No dashboard, no auth, no metrics file, no IDE skills, no `init` scaffolding.**

## Install

```bash
cd /Users/aadityagaur/projects/agent-startup/checkpoint
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Run the demo

```bash
export OPENAI_API_KEY=sk-...
checkpoint run example/scenario.md --harness "python example/harness.py"
```

## Layout

```
checkpoint/
├── pyproject.toml
├── checkpoint/
│   ├── cli.py          # `checkpoint run ...`
│   ├── scenario.py     # markdown parser
│   ├── runner.py       # subprocess + twin lifecycle + evaluation
│   ├── checker.py      # [D] regex + state checks
│   ├── judge.py        # [P] LLM judge
│   └── twins/
│       └── github.py   # FastAPI GitHub twin (in-memory state, trace endpoint)
└── example/
    ├── scenario.md     # Create-an-issue demo
    └── harness.py      # OpenAI agent using the twin
```

## Env vars the harness receives

| Var | Source |
|-----|--------|
| `CHECKPOINT_TASK` | Scenario `## Prompt` or `--task` |
| `CHECKPOINT_BASE_URL` | URL of the running twin |
| `CHECKPOINT_GITHUB_URL` | Same as base, namespaced by clone |
| `ARCHAL_ENGINE_TASK` | Alias for drop-in Archal harness compatibility |
| `ARCHAL_ENGINE_MODE` | Always `"local"` |
