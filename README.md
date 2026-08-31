# Checkpoint

Test your AI agent against stateful synthetic GitHub, Slack, Stripe, Linear, Supabase, Discord, and Google Workspace — then gate the build on the result. Your agent calls the production URLs unmodified; Checkpoint intercepts at the TLS layer, routes to a local twin, records every call, and scores the run 0–100 with deterministic + LLM-judged criteria.

**The release gate for AI agents.** Hand-written evals miss the long tail, and production is the wrong place to learn that your agent refunds an ineligible order under social pressure. Checkpoint runs your agent through stateful, multi-step scenarios against twins that hold real state and respond wire-compatibly — happy paths, edge cases, adversarial inputs, policy boundaries — scores each run, and fails your CI when the average drops below your threshold. The code path you ship is the code path you test.

**Status:** v0.1.0 · open source (Apache-2.0) · 7 twins · 16 scenarios · 4 example agents

## Install

```bash
pip install checkpoint-agents      # installs the `checkpoint` CLI
export OPENAI_API_KEY=sk-...        # used by the LLM judge for [P] criteria
```

Requires **Python ≥ 3.11**. Docker is optional (used for full real-SDK fidelity — see *Mental model*). Until the package is published to PyPI you can install from source: `pip install git+https://github.com/Aaditya2605/checkpoint`.

**Vendor-neutral.** The judge works with any model — pass `--model` (or set `defaults.judge_model`) to a `gpt-*`, `claude-*`, or `gemini-*` name, or point `CHECKPOINT_LLM_BASE_URL` at any OpenAI-compatible endpoint (local, vLLM, OpenRouter). Claude needs `pip install checkpoint-agents[anthropic]`; Gemini and compatible endpoints need nothing extra.

## Quickstart

```bash
# Run a bundled scenario against a bundled agent.
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs
```

The first Docker run builds a small TLS-sidecar image once (~1–2 min); after that a run takes seconds. You'll see the agent's stderr stream live, then a scored criterion table. Open the dashboard for the same view with history and comparison:

```bash
checkpoint serve   # http://127.0.0.1:4001
```

No Docker? Add `--no-docker` for fast subprocess mode (your agent reads `CHECKPOINT_<CLONE>_URL` instead of hitting intercepted production URLs).

## Mental model

Checkpoint is a loop with four moving parts:

1. **Twins** — stateful synthetic SaaS APIs that run locally and hold real state across a multi-step run.
2. **Harness** — your agent, referenced by a command (zero-code) or a Docker image. Checkpoint never modifies your code.
3. **Scenario** — a markdown file: a `## Setup` seed, a `## Prompt` task, and `## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged).
4. **Gate** — run each scenario N times, score every run 0–100, and exit non-zero if the average falls below your `--pass-threshold`. That exit code is the whole point: it blocks a bad build in CI.

Your agent talks to production URLs; in Docker mode a mitmproxy sidecar transparently routes those calls to the twins, so real SDKs work unmodified. In `--no-docker` mode the twins run as local processes and your agent reads their URLs from env vars.

## Highlights

- **Zero-code integration.** Your agent doesn't import Checkpoint or change a line. Point us at the command that runs it: `checkpoint init --command "python my_agent.py"`. Checkpoint handles task injection (env / arg / stdin) and stdout capture.
- **7 SaaS twins** with REST + MCP tool surfaces, named seeds, and runtime knobs (rate-limit, read-only, permissions-denied). Six are wire-compatible with the official SDKs; the **Linear** twin currently exposes a REST-style + MCP surface (the official GraphQL SDK is not yet supported).
- **Failure-first dashboard.** Browse runs, compare scores, watch scenarios stream live, manage twin state. A failed run leads with a red "What went wrong" card: each failed criterion plus the judge's reasoning.
- **Deterministic + LLM grading.** `[D]` criteria are checked against twin state by a deterministic catalog (free, no LLM) where a matching check exists; unrecognized phrasings fall back to a schema-validated LLM parse. `[P]` criteria are graded by the judge model (default `gpt-4o-mini`).
- **16 bundled scenarios** covering happy paths, adversarial inputs, and multi-clone cross-system flows.

## Test your own agent

The zero-code path — your agent code is never modified.

```bash
cd /your/agent/repo
checkpoint init --command "python my_agent.py"
```

This writes `harness.json` (how to invoke your agent), `.checkpoint.json` (project defaults), and `scenarios/quickstart.md` (a starter). Checkpoint sets `CHECKPOINT_TASK=<scenario prompt>` and runs your command. Delivery modes cover any agent shape:

| Your agent reads the prompt from… | Flag |
|---|---|
| Env var `CHECKPOINT_TASK` (default) | _(no flag)_ |
| A custom env var | `--task-env MY_VAR` |
| A CLI arg | `--task-via arg --task-arg --prompt` |
| Stdin | `--task-via stdin` |

`--task-via arg` and `stdin` require `--no-docker` today (Docker mode delivers the task via env var). Your agent prints its final answer to stdout — JSON (`{"text": "..."}`) or plain text — and exits 0 on success.

## Writing scenarios

```markdown
# Quickstart

## Setup
Use the small-project seed.

## Prompt
File a GitHub issue in `acme/webapp` titled "Login broken" with the symptom.

## Success Criteria
- [D] An issue titled "Login broken" exists
- [D] The issue is in the open state
- [T] no redundant calls
- [P] The agent's final answer references the new issue number

## Config
clones: github
```

`[D]` checks final twin state, `[P]` is LLM-judged, and **`[T]` scores the agent's trajectory** — the actual sequence of API calls, deterministically and for free (`at most N calls`, `no failed calls`, `no redundant calls`, `did not call DELETE`). Output-only checks miss the agent that reaches the right end state through a wasteful or unsafe path. Lint with `checkpoint validate scenarios/my-test.md`.

## Use it from your coding agent (MCP)

`checkpoint mcp` runs Checkpoint as an MCP server over stdio, exposing `list_scenarios`, `run_scenario`, and `gate` as tools. Register it with Claude Code / Cursor (command `checkpoint`, args `["mcp"]`) and the agent can test — and gate — the very agent it's writing, inline, without leaving the editor.

## The dashboard

`checkpoint serve` boots a local web UI at `http://127.0.0.1:4001`:

- **Failure-first run inspection** — failed runs lead with each failed criterion, the judge's reasoning, and the agent's final answer.
- **Live run streaming** over Server-Sent Events; **two-up comparison**; **live twin management** (start/stop/seed/reset, list MCP tools); **anonymized download** (emails, PATs, and keys regex-redacted).

**Running it safely:** the dashboard binds to `127.0.0.1` and needs no auth there. If you bind it anywhere else you must set `CHECKPOINT_DASHBOARD_API_KEY` (it refuses to start on a non-loopback bind without one). `POST /api/jobs` runs agent harnesses — only point it at code you trust, and use `CHECKPOINT_DASHBOARD_READ_ONLY=1` for viewer-only instances.

## The gate

Agents are non-deterministic, so a single green run is a coin flip, not a verdict. `checkpoint gate` runs each scenario N times and decides from the **distribution** of outcomes — a Wilson confidence interval on the pass rate — not one lucky run:

```bash
checkpoint gate scenarios/ --harness "python my_agent.py" -n 20
```

Each scenario is classified `stable_pass` / `flaky` / `stable_fail` / `regression`, and the run gets one verdict: **SHIP** (every scenario confidently passes), **BLOCK** (any confident failure/regression — exit 1), or **CONDITIONAL** (something's flaky; exit 0, or 1 with `--strict`). Tune with `--ship-min` / `--block-max` / `--pass-threshold`. Pass rates are remembered per scenario, so a build that *used* to pass and now fails reads as a **regression**, not just a failure (`--no-baseline` to disable).

Add `--certificate cert.json` to issue a **signed Trust Certificate** — the verdict, the per-scenario statistical evidence, and the agent/commit/model it was tested against, sealed with Ed25519. `checkpoint cert verify cert.json` proves it wasn't altered. Attach it to a release or hand it to a reviewer.

## Red-teaming

`checkpoint redteam --harness "python my_agent.py"` runs an adversarial pack — scenarios mapped to the **OWASP Agentic Top 10** where a passing agent is one that *resists* (refuses the destructive instruction, ignores the injected command, declines to exfiltrate) — and reports which attack categories your agent is vulnerable to. Exit 1 if any attack lands. Tag your own adversarial scenarios with `owasp: ASI04` in `## Config` to include them, or generate new ones: `checkpoint gen-attacks <base-scenario> --out scenarios/redteam` asks a model to invent adversarial variations across OWASP categories (review them before gating — generated attacks are candidates, not verdicts).

## Simulated users

A single prompt tests a single exchange; real users push back, clarify, and get impatient. `checkpoint simulate <scenario> --harness "..." --goal "..."` drives an LLM **persona** through a multi-turn conversation with your agent against stateful twins (state accumulates turn over turn), then scores whether the goal was met. Because simulated users are imperfect proxies for humans, every run reports a **calibration confidence** — the score is never presented as ground truth. Use `--persona`, `--tone`, `--patience`, and `--adversarial` to shape the user.

## CI integration

```yaml
- name: Gate the agent
  run: checkpoint gate scenarios/ --harness "python my_agent.py" -n 20 -o json
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Exit code 1 blocks the pipeline on BLOCK. (The simpler `checkpoint run scenarios/ -n 3 --pass-threshold 80` gates on the mean instead.) Run records land in `.checkpoint/cache/runs/*.json`.

## Reference

Run `checkpoint <command> --help` for full options.

| Command | Purpose |
|---|---|
| `checkpoint init --command "..."` | Scaffold integration in the current repo (zero-code) |
| `checkpoint run <scenario.md>` | Run a scenario, print the score |
| `checkpoint gate <dir/> --harness "..." -n 20` | Statistical release gate — SHIP/CONDITIONAL/BLOCK from N-run pass-rate CIs |
| `checkpoint gate ... --certificate cert.json` / `checkpoint cert verify cert.json` | Issue / verify a signed Trust Certificate |
| `checkpoint redteam --harness "..."` | Run the OWASP Agentic Top 10 adversarial pack; report vulnerabilities |
| `checkpoint simulate <scenario> --harness "..."` | Multi-turn simulated-user conversation with a calibration confidence |
| `checkpoint run <dir/> -n 3 --pass-threshold 80` | Simpler mean-based CI gate |
| `checkpoint serve` | Start the web dashboard |
| `checkpoint mcp` | Run Checkpoint as an MCP server so a coding agent can test the agent it's building |
| `checkpoint validate <scenario.md>` | Lint a scenario |
| `checkpoint clone start \| stop \| seed \| reset <id>` | Manage long-lived twin sessions |
| `checkpoint compare <run_a> <run_b>` | Criterion-level diff between two runs |
| `checkpoint doctor` | Verify environment (Python, Docker, sidecar image, API key) |

## Roadmap

Statistical gating (N-run confidence intervals, flake vs. regression), vendor-neutral judging, signed Trust Certificates, OWASP-Agentic red-team packs, and trajectory-level `[T]` scoring ship today. Next: automated adversarial generation, persona calibration against real transcripts, a full SQLite run store, and organization-rooted certificate signing. Follow along or contribute — see `CONTRIBUTING.md`.

## Contact

[usecheckpoint.dev](https://usecheckpoint.dev) · hello@usecheckpoint.dev

---

Apache-2.0 — see `LICENSE`. Hosted and cloud components are separate and not covered by this license.
