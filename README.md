# checkpoint

**Test your AI agent against stateful synthetic GitHub / Slack / Stripe — score it 0-100 with deterministic + LLM checks. No real-API credits burned.**

Drop a markdown scenario in your repo, point Checkpoint at your harness, run `checkpoint run scenario.md`, get a graded report. Works with any agent — LangChain, OpenAI SDK, Anthropic tools, raw `requests` — Checkpoint doesn't care what's inside.

## Install

```bash
pip install -e .
pip install mitmproxy docker fastapi uvicorn
export OPENAI_API_KEY=sk-...
```

## Quickstart

```bash
# 1. Scaffold a Checkpoint integration in your repo (creates harness.py,
#    .checkpoint.json, .claude/skills/checkpoint/SKILL.md, scenario.md):
checkpoint init

# 2. Edit harness.py to wire in your agent. Skip this on first run to use
#    the stub harness for a smoke test.

# 3. Run the starter scenario:
checkpoint run scenario.md

# 4. Or run the bundled multi-clone demo (GitHub + Slack + Stripe in one run):
checkpoint run example/scenarios/multi-clone-demo.md
```

Output looks like this:

```
checkpoint run — scenario.md
clone: github
runs:  1
judge: gpt-4o-mini (default)

Run 1/1
  ✓ [D] An issue titled "Add login button" exists
  ✓ [P] The agent's final answer references the issue number
Score: 100/100  (2 API call(s))
```

## Mental model

A **twin** is a stateful synthetic version of a SaaS API (GitHub / Slack /
Stripe), running locally. A **scenario** is a markdown file with `## Setup`
(plain-English starting state), `## Prompt` (the task for the agent), and
`## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged checks). A
**harness** is your agent in a script that reads `CHECKPOINT_<CLONE>_URL`
env vars and prints `{"text": "final answer"}` to stdout. Checkpoint spins
up the twins, runs the harness, grades against the criteria, returns a
score.

For Docker-isolated runs with TLS interception so your agent calls
`https://api.github.com` directly:

```bash
checkpoint run scenario.md --docker
```

## CLI reference

| Command | Purpose |
|---|---|
| `checkpoint init` | Scaffold the integration in the current repo. |
| `checkpoint run <scenario.md>` | Run a scenario, get a score. |
| `checkpoint run <dir/> --tag smoke` | Filter scenarios by tag. |
| `checkpoint doctor` | Verify environment (docker, ports, API key). |
| `checkpoint scenario list` | Enumerate scenarios in cwd. |
| `checkpoint clone start github` | Spin up a long-lived twin session. |
| `checkpoint traces detail` | Inspect the last run. |

## More

- **5 bundled scenarios** under [`scenarios/`](scenarios/) — GitHub happy
  path, GitHub adversarial, Slack incident response, Stripe refunds,
  multi-clone cross-system.
- **JS test suite?** See [`checkpoint-vitest/`](checkpoint-vitest/) for the
  `@checkpoint/vitest` package.
- **Docker sandbox?** See [`checkpoint/sandbox/`](checkpoint/sandbox/) for
  the pre-baked image with the TLS sidecar attached.
- **Architecture, scope, and the why?** See
  [`../.planning/PROJECT.md`](../.planning/PROJECT.md) and
  [`../archal-docs/SCOPE.md`](../archal-docs/SCOPE.md).

## License

MIT
