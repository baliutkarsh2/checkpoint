# Set up Checkpoint in your existing agent repo

Goal: get Checkpoint testing your agent **without touching a single line of
your agent code**. Setup time: ~2 minutes.

---

## The model

You don't write a "harness" in Python. You describe **how Checkpoint should
invoke your existing agent** in a tiny `harness.json`. Checkpoint runs the
command for every scenario and injects the task via env / arg / stdin.

That's it. Your agent doesn't import `checkpoint`. Your agent doesn't even
know Checkpoint exists.

---

## 1. Install

```bash
pip install checkpoint-agents
export OPENAI_API_KEY=sk-...   # used by the LLM judge
```

Docker should be running — that's the default run mode so your agent's real
SDKs (PyGithub, supabase-py, etc.) get TLS-intercepted to local twins. For
fast iteration without Docker, pass `--no-docker` on the CLI.

---

## 2. Point Checkpoint at your agent

In your agent repo's root:

```bash
checkpoint init --command "python my_agent.py"
```

That's the whole zero-code step. `init` writes:

| File                          | What it is                                              |
|-------------------------------|---------------------------------------------------------|
| `harness.json`                | Declarative spec describing how to run your agent       |
| `.checkpoint.json`            | Project defaults (clones, judge model)                  |
| `scenarios/quickstart.md`     | Starter scenario you can edit                           |
| `.gitignore`                  | Adds `.checkpoint/` for cache + run records             |

**No Python file gets written into your repo.** Your code stays untouched.

### How your agent receives the task

By default, Checkpoint sets the env var `CHECKPOINT_TASK` to the scenario's
prompt before running your command. Three other delivery modes:

| Need                                      | Flag                                         |
|-------------------------------------------|----------------------------------------------|
| Pass as a CLI arg                         | `--task-via arg --task-arg --prompt`         |
| Pipe via stdin                            | `--task-via stdin`                           |
| Wire it some other way (you handle it)    | `--task-via none`                            |
| Use a custom env var name                 | `--task-env MY_PROMPT_VAR`                   |

Examples:

```bash
# Your agent already reads CHECKPOINT_TASK (or any env var you name):
checkpoint init --command "python my_agent.py"

# Your agent takes the task as a CLI flag:
checkpoint init --command "node agent.js" --task-via arg --task-arg --prompt

# Your agent reads the task from stdin:
checkpoint init --command "./run-agent.sh" --task-via stdin

# Use your agent's existing Dockerfile for real-SDK fidelity:
checkpoint init --command "python my_agent.py" --dockerfile ./Dockerfile
```

---

## 3. Write or pick a scenario

A scenario is a markdown file with a prompt and pass/fail criteria.
`checkpoint init` drops a starter at `scenarios/quickstart.md`:

```markdown
# Quickstart

## Prompt
File a GitHub issue in `acme/webapp` titled "Login broken" with the symptom.

## Success Criteria
- [D] An issue titled "Login broken" exists
- [P] The agent's final answer references the new issue number

## Config
clones: github
runs: 1
timeout: 60
```

`[D]` = deterministic (regex/state lookup, free). `[P]` = perception
(GPT-judged, ~1 LLM call). Mix and match.

To use the bundled scenarios that ship with Checkpoint:

```bash
checkpoint scenario list                # 16 bundled ones
checkpoint validate scenarios/<file>.md # lint before running
```

---

## 4. Run it

```bash
# CLI:
checkpoint run scenarios/quickstart.md

# Dashboard (recommended for iteration):
checkpoint serve   # http://127.0.0.1:4001
```

From the dashboard you can:
- Browse every past run and see **exactly what failed and why** — the Process tab leads with a red "What went wrong" card listing each failed criterion + the judge's reasoning
- Compare two runs side-by-side
- Launch new runs and watch them stream live
- Manage live clones (start/stop/seed/reset) without leaving the browser
- Pick agent + scenario from dropdowns and hit Run

---

## Output contract (only one rule)

Your agent should print its final answer to stdout. Two acceptable shapes:

```python
# JSON (recommended — clearer for the dashboard's "final answer" view):
print(json.dumps({"text": "Created issue #42"}))

# Or plain text — anything you print is treated as the final answer:
print("Created issue #42")
```

Exit 0 on success, non-zero on agent failure.

If your agent already prints SOMETHING to stdout at the end of its run, no
change needed.

---

## CI integration

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
records land in `.checkpoint/cache/runs/*.json`; upload them as artifacts
and a shared dashboard can read them.

---

## Inline run (skip init entirely)

For one-off testing, you can skip `init` and pass `--command` directly:

```bash
checkpoint run scenarios/github-happy-path.md \
  --command "python my_agent.py" \
  --no-docker
```

No `harness.json` file needed at all.

---

## Troubleshooting

| Symptom                                  | Look here                                              |
|------------------------------------------|--------------------------------------------------------|
| `Docker daemon not reachable`            | Start Docker, or pass `--no-docker` for fast iteration |
| `Harness executable not found`           | Your `--command` references a missing binary — verify the path |
| Score is 0/100, exit code -1             | Your agent crashed before printing anything — check the Process tab in the dashboard for stderr |
| Score is 0/100 but agent looks correct   | Your scenario has no `## Success Criteria` section, or your agent's output didn't match the expected shape |
| Real SDK calls return `Bad credentials`  | Docker sidecar intercept didn't fire — re-check that your Dockerfile inherits the CA cert (the example agents under `examples/agents/` show how) |

For everything else: `checkpoint doctor` (CLI) or **Setup → Doctor** in the
dashboard.

---

## Existing-style harness (Python file, opt-in)

If you'd rather have a starter Python harness you can edit:

```bash
checkpoint init --template openai-agents
# templates: raw | anthropic | openai-agents | langchain
```

This writes a `harness.py` you wire to your agent. The four example agents
under [`examples/agents/`](examples/agents/) are full Dockerized references
in this style.
