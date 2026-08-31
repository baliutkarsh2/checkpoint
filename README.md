# Checkpoint

[![CI](https://img.shields.io/github/actions/workflow/status/baliutkarsh2/checkpoint/checkpoint-ci.yml?branch=main&label=CI)](https://github.com/baliutkarsh2/checkpoint/actions/workflows/checkpoint-ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/baliutkarsh2/checkpoint/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://github.com/baliutkarsh2/checkpoint/blob/main/pyproject.toml)

**The release gate for AI agents.** Run your real agent — unmodified — N times against real tools, score every run, and let a statistical verdict decide: **SHIP** or **BLOCK**. Checkpoint is the CI gate that fails the build *before* a flaky agent reaches production.

Agents are non-deterministic, so one green demo run is a coin flip, not a verdict. Hand-written evals miss the long tail, and production is the wrong place to learn that your agent refunds an ineligible order under social pressure. Checkpoint runs each scenario N times, scores every run 0–100 (deterministic checks + an LLM judge), and gates on the **distribution** of outcomes — a Wilson confidence interval on the pass rate — not one lucky pass. In Docker mode your agent calls its real APIs unmodified — Checkpoint intercepts at the TLS layer and routes each call to a local stateful twin, so **the code path you ship is the code path you test**. (`checkpoint gate` runs scenarios in subprocess mode today, where your agent reads twin URLs from env; TLS-intercept for the gate is on the roadmap.)

```bash
pip install git+https://github.com/baliutkarsh2/checkpoint   # PyPI release pending
checkpoint demo          # deterministic, offline, no API key — see it score in ~5s
```

**Status:** v0.1.0 · open source (Apache-2.0) · statistical gate · trajectory scoring · signed evidence

## Install

```bash
pip install git+https://github.com/baliutkarsh2/checkpoint   # installs the `checkpoint` CLI
export OPENAI_API_KEY=sk-...        # only needed for [P] LLM-judged criteria
```

**Not on PyPI yet.** The distribution will be `checkpoint-agents` once the first release is tagged; until then use the source install above. Note that the bare name `checkpoint` on PyPI is an unrelated project — always install `checkpoint-agents`, never the bare name.

Requires **Python ≥ 3.11**. Docker is optional (used for full real-SDK fidelity — see *Mental model*). The source install ships **without the dashboard bundle** (it's built in CI), so `checkpoint serve` needs a one-time `cd checkpoint/dashboard/web && npm ci && npm run build` (Node 22+) first. The CLI, including `checkpoint demo`, works without it.

**Vendor-neutral.** The judge works with any model — pass `--model` (or set `defaults.judge_model`) to a `gpt-*`, `claude-*`, or `gemini-*` name, or point `CHECKPOINT_LLM_BASE_URL` at any OpenAI-compatible endpoint (local, vLLM, OpenRouter). Claude needs `pip install checkpoint-agents[anthropic]`; Gemini and compatible endpoints need nothing extra.

## Quickstart

**1. See it work — no Docker, no API key.**

```bash
checkpoint demo
```

Runs a bundled deterministic scenario against a bundled agent entirely offline (the LLM judge never runs), and prints a green criterion table with `Score: 100/100` in a few seconds. This is the "does it work?" proof.

**2. Test a real agent** against a stateful twin with full SDK fidelity (needs Docker + an API key):

```bash
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs
```

The first Docker run builds a small TLS-sidecar image once (~1–2 min); after that a run takes seconds. You'll see the agent's stderr stream live, then a scored criterion table. No Docker? Add `--no-docker` for fast subprocess mode (your agent reads `CHECKPOINT_<CLONE>_URL` instead of hitting intercepted production URLs).

**3. Open the dashboard** for the same view with history and comparison:

```bash
checkpoint serve   # http://127.0.0.1:4001
```

## Mental model

Checkpoint is a loop with four moving parts:

1. **Twins** — stateful synthetic SaaS APIs (GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace) that run locally and hold real state across a multi-step run. They are **wire-shaped**: they reproduce the endpoints your scenarios exercise — the GitHub twin emits `Link` pagination headers, the Stripe twin parses the SDKs' nested/array form encoding — but not every corner of each API (Linear is REST/MCP, not the official GraphQL SDK). Fault-injection varies by twin: `rate_limit` (GitHub, Stripe, Linear, Supabase), `read_only` and `permissions_denied` (GitHub). Reach for a twin when you need to *inject faults you can't safely record*.
2. **Harness** — your agent, referenced by a command (zero-code) or a Docker image. Checkpoint never modifies your code.
3. **Scenario** — a markdown file: a `## Setup` seed, a `## Prompt` task, and `## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged).
4. **Gate** — run each scenario N times, score every run 0–100, and exit non-zero if the average falls below your `--pass-threshold`. That exit code is the whole point: it blocks a bad build in CI.

Your agent talks to production URLs; in Docker mode a mitmproxy sidecar transparently routes those calls to the twins, so real SDKs work unmodified. In `--no-docker` mode the twins run as local processes and your agent reads their URLs from env vars.

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

Add `--certificate cert.json` to issue a **signed Trust Certificate** — the verdict, the per-scenario statistical evidence, and the agent/commit/model it was tested against, sealed with Ed25519. `checkpoint cert verify cert.json` proves it wasn't altered.

`checkpoint compliance --certificate cert.json --redteam redteam.json --out report.md` rolls the gate certificate and red-team results into an **Agent Assurance Report** — a graded APPROVED / CONDITIONAL / REJECTED verdict with the statistical evidence and OWASP Agentic / NIST AI RMF / EU AI Act cross-references — the document a compliance reviewer or a customer's vendor-review team actually asks for.

## Red-teaming

`checkpoint redteam --harness "python my_agent.py"` runs an adversarial pack where a passing agent is one that *resists* (refuses the destructive instruction, ignores the injected command, declines to exfiltrate), and reports which attack categories your agent is vulnerable to. Exit 1 if any attack lands. The catalog maps scenarios to the full **OWASP Agentic Top 10** (ASI01–ASI10), but the *bundled* pack currently ships a single ASI04 (tool-misuse) probe — bring your own tagged scenarios or generate them with `gen-attacks` for broader coverage. Tag your own adversarial scenarios with `owasp: ASI04` in `## Config` to include them, or generate new ones: `checkpoint gen-attacks <base-scenario> --out scenarios/redteam` asks a model to invent adversarial variations across OWASP categories (review them before gating — generated attacks are candidates, not verdicts).

## Simulated users

A single prompt tests a single exchange; real users push back, clarify, and get impatient. `checkpoint simulate <scenario> --harness "..." --goal "..."` drives an LLM **persona** through a multi-turn conversation with your agent against stateful twins (state accumulates turn over turn), then scores whether the goal was met. Because simulated users are imperfect proxies for humans, every run reports a **plausibility signal** — a transparent heuristic on the conversation's shape (turns taken, whether the persona gave up), so the score is never mistaken for ground truth. Use `--persona`, `--tone`, `--patience`, and `--adversarial` to shape the user.

## CI integration

Drop the GitHub Action into your workflow:

```yaml
- uses: baliutkarsh2/checkpoint@main
  with:
    target: scenarios/
    harness: "python my_agent.py"
    runs: "20"
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Or call the CLI directly: `checkpoint gate scenarios/ --harness "python my_agent.py" -n 20`. Either way, exit code 1 blocks the pipeline on BLOCK (add `strict: true` / `--strict` to also block on CONDITIONAL). The gate reports **pass^k** — the unbiased estimate that k independent runs all pass — so a 90%-pass agent reads honestly as pass^10 ≈ 35%, not a reassuring "90%". Run records land in `.checkpoint/cache/runs/*.json`.

## Reference

Full guides live in **[docs/](https://github.com/baliutkarsh2/checkpoint/blob/main/docs/)** — [integrate your agent](https://github.com/baliutkarsh2/checkpoint/blob/main/docs/integrate-your-agent.md), [architecture](https://github.com/baliutkarsh2/checkpoint/blob/main/docs/architecture.md), [self-hosting](https://github.com/baliutkarsh2/checkpoint/blob/main/docs/self-hosting.md). Run `checkpoint <command> --help` for full options.

| Command | Purpose |
|---|---|
| `checkpoint init --command "..."` | Scaffold integration in the current repo (zero-code) |
| `checkpoint run <scenario.md>` | Run a scenario, print the score |
| `checkpoint gate <dir/> --harness "..." -n 20` | Statistical release gate — SHIP/CONDITIONAL/BLOCK from N-run pass-rate CIs |
| `checkpoint gate ... --certificate cert.json` / `checkpoint cert verify cert.json` | Issue / verify a signed Trust Certificate |
| `checkpoint redteam --harness "..."` | Run the adversarial pack (OWASP-Agentic catalog; one ASI04 probe bundled); report vulnerabilities |
| `checkpoint simulate <scenario> --harness "..."` | Multi-turn simulated-user conversation with a calibration confidence |
| `checkpoint run <dir/> -n 3 --pass-threshold 80` | Simpler mean-based CI gate |
| `checkpoint serve` | Start the web dashboard |
| `checkpoint mcp` | Run Checkpoint as an MCP server so a coding agent can test the agent it's building |
| `checkpoint redteam-mcp` | Serve a poisoned MCP server (tool-poisoning / injection techniques from the OWASP MCP Top 10) to test MCP-attack resistance |
| `checkpoint validate <scenario.md>` | Lint a scenario |
| `checkpoint clone start \| stop \| seed \| reset <id>` | Manage long-lived twin sessions |
| `checkpoint compare <run_a> <run_b>` | Criterion-level diff between two runs |
| `checkpoint db migrate` / `checkpoint db list` | Import run records into the SQLite store and query them |
| `checkpoint otel <trace.json>` | Summarize an agent's trajectory from an OpenTelemetry GenAI trace |
| `checkpoint doctor` | Verify environment (Python, Docker, sidecar image, API key) |

## Roadmap

Statistical gating (N-run confidence intervals, flake vs. regression), vendor-neutral judging, signed Trust Certificates, the OWASP-Agentic red-team catalog with automated adversarial generation, trajectory-level `[T]` scoring, and a SQLite run store all ship today. Next: **TLS-intercept (Docker) mode for `checkpoint gate`** so the gate tests the exact shipped code path, **broader bundled red-team coverage** across the remaining ASI categories, **record/replay cassettes** (capture your agent's real API traffic once, replay it deterministically — twins become the fault-injection layer), **judge calibration** against a human gold set with `pass^k` reliability reporting, persona calibration against real transcripts, and organization-rooted certificate signing. Follow along or contribute — see [CONTRIBUTING](https://github.com/baliutkarsh2/checkpoint/blob/main/CONTRIBUTING.md).

## Contact

[usecheckpoint.dev](https://usecheckpoint.dev) · hello@usecheckpoint.dev

---

Apache-2.0 — see [LICENSE](https://github.com/baliutkarsh2/checkpoint/blob/main/LICENSE). [Contributing](https://github.com/baliutkarsh2/checkpoint/blob/main/CONTRIBUTING.md) · [Code of Conduct](https://github.com/baliutkarsh2/checkpoint/blob/main/CODE_OF_CONDUCT.md) · [Security](https://github.com/baliutkarsh2/checkpoint/blob/main/SECURITY.md) · [Changelog](https://github.com/baliutkarsh2/checkpoint/blob/main/CHANGELOG.md). Hosted and cloud components are separate and not covered by this license.
