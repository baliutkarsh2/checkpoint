# Set up Checkpoint in your existing agent repo

This is the canonical guide for **integrating Checkpoint into an agent you've
already built**. If you're starting from scratch, follow the
[README quickstart](README.md) instead.

Total setup time: **~5 minutes** for the first scenario to run.

---

## Prerequisites

- Python 3.10+
- Docker running (default run mode — your agent's real SDKs get
  TLS-intercepted to local twins)
- An OpenAI API key for the LLM judge:
  `export OPENAI_API_KEY=sk-...`

> Don't have Docker handy? Pass `--no-docker` to run in subprocess mode.
> Your agent will need to read `CHECKPOINT_<CLONE>_URL` env vars instead of
> hitting production URLs. Subprocess mode is fast but doesn't exercise the
> real SDK code path.

---

## 1. Install

```bash
pip install checkpoint
```

You can do this in your project's venv or globally. The package brings its
own `checkpoint` CLI command.

---

## 2. Scaffold into your repo

From your agent repo's root:

```bash
cd /path/to/your-agent-repo
checkpoint init --template openai-agents
```

Templates available:

| Template          | When to use                                    |
|-------------------|------------------------------------------------|
| `openai-agents`   | OpenAI Agents SDK + MCP                        |
| `anthropic`       | Anthropic Claude + MCP                         |
| `langchain`       | LangChain ReAct                                |
| `raw`             | Plain `requests` — any framework, any language |

This writes:

```
your-repo/
├── .checkpoint.json          # config (clones, default harness, judge model)
├── harness.py                # the shim Checkpoint invokes
├── harness.json              # harness manifest (env, command)
├── scenario.md               # starter scenario you can edit
├── .claude/
│   ├── skills/checkpoint/SKILL.md          # for Claude Code users
│   └── commands/checkpoint-test.md         # `/checkpoint-test` slash command
└── (your existing files untouched)
```

Existing files are **never overwritten** — re-running `checkpoint init` is safe.

---

## 3. Wire `harness.py` to your agent

Open `harness.py` and edit the agent loop to call into your existing code.
The contract is:

1. **Read** the task from `CHECKPOINT_TASK` env var.
2. **Call your agent** with that task.
3. **Print** `{"text": "...final answer..."}` to stdout.
4. **Exit 0** on success.

Example:

```python
import json, os, sys
from my_agent import answer  # ← your existing code

task = os.environ["CHECKPOINT_TASK"]
result = answer(task)
print(json.dumps({"text": result}))
```

If your agent uses real SDKs (PyGithub, supabase-py, slack_sdk, etc.) in
Docker mode, **they'll work unmodified** — the Checkpoint sidecar TLS-
intercepts production URLs and routes them to the twins. Your agent doesn't
need to know about Checkpoint.

> **Look at [`examples/agents/`](examples/agents/) for four full reference
> implementations** (OpenAI tools, Anthropic tools, LangChain ReAct, MCP
> client) — each is a complete Dockerized harness you can copy and adapt.

---

## 4. Pick or write a scenario

Scenarios live as markdown files. The `init` command drops a starter at
`scenario.md`:

```markdown
# My first scenario

## Prompt
File a GitHub issue in `acme/webapp` titled "Login broken" with the symptoms.

## Success Criteria
- [D] An issue titled "Login broken" exists
- [P] The agent's final answer references the new issue number

## Config
clones: github
runs: 1
timeout: 60
```

`[D]` = deterministic (regex/state lookup, free). `[P]` = perception
(GPT-judged, ~1 LLM call). You can mix and match.

To use the **bundled** scenarios that ship with Checkpoint:

```bash
checkpoint scenario list                          # lists 16 bundled ones
checkpoint validate scenarios/<file>.md           # lint before running
```

---

## 5. Run it

CLI:

```bash
checkpoint run scenario.md
# or, for the bundled ones:
checkpoint run scenarios/github-happy-path.md
```

Dashboard (recommended for iteration):

```bash
checkpoint serve         # opens http://127.0.0.1:4001
```

From the dashboard you can:
- See every past run with full agent conversation, tool calls, twin state
- Compare two runs side-by-side
- Launch new runs and watch them stream live
- Manage live clones (start/stop/seed/reset) without leaving the browser
- Browse scenarios and agents with click-through detail pages

---

## CI integration

Once a scenario is green locally, add Checkpoint to CI:

```yaml
- name: Run Checkpoint scenarios
  run: |
    checkpoint run scenarios/ \
      -n 3 \
      --pass-threshold 80 \
      -o json -q \
      --no-failure-analysis
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Exit code 1 if any scenario's avg score drops below the threshold. Run
records are written to `.checkpoint/cache/runs/*.json`; upload them as
artifacts and a shared dashboard can read them.

---

## Troubleshooting

| Symptom                                  | Look here                                              |
|------------------------------------------|--------------------------------------------------------|
| `Docker daemon not reachable`            | Start Docker, or pass `--no-docker` for fast iteration |
| Real SDK calls return `Bad credentials`  | Sidecar intercept didn't fire — re-check `harness.py`'s entrypoint combines system + sidecar CAs |
| Score is 0/100 but tests look correct    | Scenario has no `## Success Criteria` section          |
| Dashboard shows `agent=unknown` on runs  | The run record was written by an older CLI version — re-run with v0.2+ |
| Score is 100/100 but you don't trust it  | Open the run in the dashboard, inspect the conversation in the Chat tab, replay tool calls one by one |

For everything else: `checkpoint doctor` (CLI) or **Setup → Doctor** in the
dashboard.
