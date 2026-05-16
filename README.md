# Checkpoint

Test your AI agent against stateful synthetic GitHub, Slack, Stripe, Linear, Supabase, Discord, and Google Workspace. Your agent calls the production URLs unmodified; Checkpoint intercepts at the TLS layer and routes to a local twin. Score the result 0-100 with deterministic + LLM-judged criteria.

**Why this exists.** You can't reliably test agent reasoning against real SaaS — rate limits, real mutations, leaked data, real money. Mock-based unit tests miss the bug class that actually breaks agents in production: stateful, multi-step sequences whose correctness depends on what the SaaS returned three calls ago. Checkpoint runs those sequences against twins that hold real state and respond wire-compatibly, so the code path you ship is the code path you test.

**Status:** v0.1.0 · active development · 7 twins · 16 scenarios · 4 example agents

## Highlights

- **Zero-code integration.** Your agent doesn't import Checkpoint or change a single line. Point us at the command that runs your agent: `checkpoint init --command "python my_agent.py"`. Checkpoint handles task injection (env / arg / stdin) and stdout capture.
- **7 SaaS twins** wire-compatible with production: GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace. Each runs locally as a FastAPI app with REST + MCP tool surfaces, named seeds, and runtime knobs (rate-limit, read-only, permissions-denied).
- **Failure-first dashboard.** Browse runs, compare scores, watch scenarios stream live, manage twin state. A failed run leads with a red "What went wrong" card listing each failed criterion plus the judge's reasoning.
- **Deterministic + LLM grading.** Mix `[D]` regex/state checks (free, fast) with `[P]` LLM-judged criteria (default model: `gpt-4o-mini`). Schema-validated JSON parser keeps the judge honest.
- **16 bundled scenarios** covering happy paths, adversarial inputs, and multi-clone cross-system flows.

## Quickstart

```bash
pip install checkpoint
export OPENAI_API_KEY=sk-...   # used by the LLM judge for [P] criteria

# Run a bundled scenario against a bundled agent — ~30 seconds, no setup.
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs
```

You'll see the agent's stderr stream live, then a scored criterion table. Open the dashboard for the same view with history and comparison:

```bash
checkpoint serve   # http://127.0.0.1:4001
```

<!-- TODO: drop a 10-second GIF of dashboard LiveRun → score panel, or a screenshot of RunDetail's failure-first card -->

**Requirements:** Python ≥ 3.11, Docker running. Docker is the default mode — your agent's real SDKs hit twins via TLS intercept. Pass `--no-docker` for fast subprocess mode without real-SDK fidelity.

## Test your own agent

The zero-code path. Your agent code is never modified.

```bash
cd /your/agent/repo
checkpoint init --command "python my_agent.py"
```

This writes:

- `harness.json` — declarative spec describing how to invoke your agent
- `.checkpoint.json` — project defaults (twins, judge model)
- `scenarios/quickstart.md` — starter scenario

**No Python file is added to your repo.** Your agent doesn't import `checkpoint`.

By default, Checkpoint sets `CHECKPOINT_TASK=<scenario prompt>` and runs your command. Four delivery modes cover any agent shape:

| Your agent reads the prompt from… | Flag |
|---|---|
| Env var `CHECKPOINT_TASK` (default) | _(no flag)_ |
| A custom env var | `--task-env MY_VAR` |
| A CLI arg | `--task-via arg --task-arg --prompt` |
| Stdin | `--task-via stdin` |

**Output contract:** your agent prints its final answer to stdout — JSON (`{"text": "..."}`) or plain text both work. Exit 0 on success.

## Core concepts

| Term | What it is |
|---|---|
| **Twin** | A stateful synthetic SaaS API running locally. Same wire protocol as production, with `/_state`, `/_reset`, `/_seed/<name>`, `/_trace`, `/_config` introspection. |
| **Scenario** | A markdown file with `## Setup`, `## Prompt`, and `## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged). |
| **Harness** | Your agent — referenced by command (`harness.json`, zero-code) or by Docker image (`Dockerfile` + entrypoint, for full-fidelity real-SDK runs). |
| **Judge** | The grader that converts a run trace into a 0-100 score using your `[D]` / `[P]` criteria. Default model: `gpt-4o-mini`. |

**Caveats up front.** Twins are functional approximations of production SaaS, not byte-identical replicas — they cover the wire shape used by every bundled example agent, but a corner of the documented surface may not be implemented. `[P]` criteria require an LLM API key, so each run has a real per-criterion cost. Docker is required for full real-SDK fidelity; without it, your agent must read `CHECKPOINT_<CLONE>_URL` env vars directly.

## How it works

```
checkpoint run scenarios/foo.md --harness-dir my-agent/
    ├── starts the TLS sidecar (mitmproxy on :443) + N twin containers in shared netns
    ├── launches your harness container with extra_hosts mapping production
    │   domains (api.github.com, slack.com, …) to the sidecar
    ├── your harness uses real SDKs against production URLs; sidecar routes them
    │   to the twins; twins record every call
    ├── collects per-twin trace + state at the end of the run
    └── 3-stage evaluator: regex patterns → schema-validated LLM-JSON → judge model
```

Every twin also mounts an MCP server at `http://localhost:<port>/<clone>/mcp`, with tool names mirroring each vendor's official MCP server. Useful for hand-driving a twin from Claude Desktop, Cursor, or any MCP host.

## Writing scenarios

A scenario is one markdown file:

```markdown
# Quickstart

## Setup
Use the small-project seed.

## Prompt
File a GitHub issue in `acme/webapp` titled "Login broken" with the symptom.

## Success Criteria
- [D] An issue titled "Login broken" exists
- [D] The issue is in the open state
- [P] The agent's final answer references the new issue number

## Config
clones: github
```

`[D]` criteria are checked deterministically against twin state — no LLM call, no cost. `[P]` criteria are graded by the judge model from the agent's final answer plus the run trace. Mix freely.

Lint a scenario before running:

```bash
checkpoint validate scenarios/my-test.md
```

The 16 bundled scenarios under `scenarios/` are the reference for both shape and coverage.

## The dashboard

The primary working surface after install. `checkpoint serve` boots a local web UI at `http://127.0.0.1:4001`:

- **Failure-first run inspection** — failed runs lead with a red "What went wrong" card: each failed criterion, the judge's reasoning, optional LLM root-cause, and the agent's final answer.
- **Live run streaming** — pick scenario + agent, hit Run, watch stdout/stderr over Server-Sent Events.
- **Two-up comparison** — tick two runs in the table, click Compare for a side-by-side diff.
- **Live twin management** — start, stop, seed, factory-reset twins from the browser; list each twin's MCP tools.
- **Anonymized download** — share a run record with emails, GitHub PATs, and OpenAI keys regex-redacted.

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

Exit code 1 if any scenario's average score drops below `--pass-threshold`. Run records land in `.checkpoint/cache/runs/*.json`.

## Reference

The most-used commands. Run `checkpoint <command> --help` for full options.

| Command | Purpose |
|---|---|
| `checkpoint init --command "..."` | Scaffold integration in the current repo (zero-code) |
| `checkpoint run <scenario.md>` | Run a scenario, print the score |
| `checkpoint run <dir/> -n 3 --pass-threshold 80` | CI mode — N runs per scenario, fail under threshold |
| `checkpoint serve` | Start the web dashboard at http://127.0.0.1:4001 |
| `checkpoint validate <scenario.md>` | Lint a scenario before running |
| `checkpoint clone start \| stop \| seed \| reset <id>` | Manage long-lived twin sessions |
| `checkpoint compare <run_a> <run_b>` | Criterion-level diff between two runs |
| `checkpoint doctor` | Verify environment (Docker, ports, API key) |

## Contact

[usecheckpoint.dev](https://usecheckpoint.dev) · hello@usecheckpoint.dev

---

© 2026 Checkpoint. Proprietary — all rights reserved.
