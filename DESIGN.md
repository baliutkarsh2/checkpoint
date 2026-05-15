# Checkpoint — Technical Design

> **Audience:** the founders.  Read top-to-bottom for the full picture, or jump to a section.
> **Scope:** every aspect of the product — concepts, architecture, code organization, distribution, testing, CI/CD, security, and where to extend.
> **Repo state at time of writing:** v0.1.0, 16K LOC of Python, 7 twins, 16 bundled scenarios, 46 test files, full SPA dashboard.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Product](#2-product)
3. [Domain model](#3-domain-model)
4. [System architecture](#4-system-architecture)
5. [The 3-stage evaluator](#5-the-3-stage-evaluator)
6. [Twin design](#6-twin-design)
7. [MCP layer](#7-mcp-layer)
8. [Scenarios — format and lifecycle](#8-scenarios--format-and-lifecycle)
9. [Run modes](#9-run-modes)
10. [CLI surface](#10-cli-surface)
11. [The dashboard](#11-the-dashboard)
12. [Distribution and packaging](#12-distribution-and-packaging)
13. [Testing strategy](#13-testing-strategy)
14. [CI/CD](#14-cicd)
15. [Security model](#15-security-model)
16. [Repository tour](#16-repository-tour)
17. [Extension points](#17-extension-points)
18. [Glossary](#18-glossary)

---

## 1. Executive summary

**Checkpoint is a black-box harness for testing AI agents against stateful synthetic copies of real SaaS APIs.**

You write a *scenario* (a markdown file with a prompt and success criteria).  Checkpoint spins up *twins* — local FastAPI servers that imitate GitHub, Slack, Stripe, Linear, Supabase, Discord, or Google Workspace — runs your agent against them, captures every API call and the resulting state, and scores the run against the criteria using a three-stage evaluator (deterministic regex → LLM-JSON → GPT judge).

There is **no real-API spend, no risk to production data, no flakiness from third-party rate limits**, and the entire stack runs locally on a developer's laptop or in CI.

**The core value props:**

- **Reproducible agent evals** — same seed, same prompt, same twins → same trace.
- **Stateful** — twins remember everything across calls (issues, channels, payment intents, rows, etc.) like the real services do, so multi-step agent workflows can be evaluated end-to-end.
- **Local-first** — no hosted backend, no login, no telemetry.  Pip-install and go.
- **Real SDKs work unmodified in Docker mode** — PyGithub against `https://api.github.com`, supabase-py against `https://*.supabase.co`, etc., transparently routed to twins via TLS interception.
- **Browseable** — bundled web dashboard with run history, score sparklines, deep trace inspector, side-by-side compare, live logs of in-flight runs, OpenAPI Swagger UI, Prometheus metrics.

**What Checkpoint is *not***:
- Not a hosted SaaS.  No remote workspace, no `archal_ws_*` tokens, no scenario marketplace.  We chose this deliberately — see [Security model](#15-security-model).
- Not an agent framework.  Bring your own loop (LangChain, Anthropic SDK, raw OpenAI tool-calling, anything).

---

## 2. Product

### Who it's for

Three personas, in priority order:

1. **Agent developers building production agents** that touch GitHub/Slack/Stripe/etc. — they need a way to iterate without a sandbox account or a refund button.
2. **Platform/infra teams** writing CI gates that say "this agent must hit ≥80/100 on these 12 scenarios before it ships."
3. **Researchers/hobbyists** who want to compare model X vs model Y on the same task without paying for either to make 50 real-API calls.

### What you get

| Capability | How it shows up |
|---|---|
| Run a scenario, get a 0-100 score | `checkpoint run scenarios/foo.md` → table with each criterion's pass/fail + score |
| Browse history, compare, see trends | `checkpoint serve` → SPA at `http://127.0.0.1:4001` |
| CI gate with threshold | `checkpoint run scenarios/ -n 3 --pass-threshold 80 -o json -q` → exit 1 if any avg < 80 |
| Real-SDK fidelity | `--docker` runs the harness in a container; mitmproxy intercepts production URLs |
| Multi-clone cross-system tests | Scenarios with `clones: github, supabase` spin up both, share state across the run |
| Long-lived twin sessions | `checkpoint clone start github` → keep a twin alive across many manual API calls |
| MCP tool surface | Every twin mounts `/mcp/` so agents using Model Context Protocol see the same tools as production |

### Bundled scenario library (16 today)

Spans happy-path, adversarial, security, and cross-system tests for every twin.  See `scenarios/` and the dashboard's Scenarios page.  Each scenario declares which twins it needs in `## Config` so the runner spins up only what's required.

---

## 3. Domain model

These are the nouns the codebase, CLI, and dashboard all share.  Internalize them once, and everything else maps cleanly.

### Twin
A **twin** is a stateful synthetic copy of a real SaaS API, implemented as a FastAPI app in `checkpoint/twins/<name>.py`.  Twins maintain in-memory state (issues, channels, payment intents, rows, etc.) and respond with wire-compatible JSON.  Each twin also exposes:

- `/_health` — liveness probe
- `/_state` — current full state snapshot (read-only inspection)
- `/_trace` — every request the agent made
- `/_reset` — clear back to a fresh empty state
- `/_seed/<name>` — load a named seed dataset
- `/_config` — runtime knobs (rate_limit, read_only, permissions_denied)
- `/mcp/` — Model Context Protocol streamable-HTTP surface

Twins are spawned as `uvicorn` subprocesses on free local ports.  The runner manages their lifetime; the user never starts them by hand (unless using `checkpoint clone start` for long-lived sessions).

**Today: 7 twins.** github, slack, stripe, linear, supabase, discord, google-workspace.

### Clone
"Clone" is the user-facing name for an *instance* of a twin running in a particular session.  When you say "Checkpoint supports the GitHub clone," you mean "the github twin is one of the supported services."  When you say "spin up a clone," you mean "start a twin process for a session."  The terms are mostly interchangeable — `clone_manager.py` deals with running clones; `twins/` contains the twin code.

### Scenario
A **scenario** is a markdown file describing one agent task and how to grade it.  The format is intentionally human-readable so non-engineers can write them.  See [Section 8](#8-scenarios--format-and-lifecycle).

### Harness
A **harness** is the user's agent script.  Checkpoint is harness-agnostic: it spawns whatever command you give it via `--harness`, sets a few env vars, reads stdout for the final answer, then evaluates.  Two contracts:

- **Subprocess mode**: the harness reads `CHECKPOINT_<CLONE>_URL` env vars and calls those URLs directly.  Simplest path.
- **Docker mode**: the harness uses real SDKs against production URLs (`https://api.github.com`); a mitmproxy sidecar intercepts and routes to twins.  Most realistic.

### Seed
A **seed** is the starting state a twin loads before the run.  Three forms:

- **Named seed** — predefined JSON under `checkpoint/twins/<clone>_seeds/`, e.g. `small-project`, `enterprise-repo`, `ecommerce`.  Reusable across scenarios.
- **Seed file** — arbitrary JSON path you supply (`--seed-file`); the runner POSTs it to the twin.
- **Setup-derived seed** — when the scenario has `## Setup` prose and `setup-seed: true`, the runner uses an LLM to generate a JSON seed from the prose.  Opt-in to keep behavior predictable.

### Criterion
A **criterion** is a single pass/fail check inside `## Success Criteria`.  Two kinds:

- **`[D]` deterministic** — regex/structural lookup against the trace + state.  Free.  Stage 1 of the evaluator.  If stage 1 can't decide, falls through to stage 2 (LLM-JSON).
- **`[P]` perception** — the GPT judge reads the agent's final answer + final state and returns pass/fail with reasoning.  ~1 LLM call.  Stage 3.

### Satisfaction score
A **score** is just `100 * passed_criteria / total_criteria` — no weighting, no half-credit.  Simple by design.  The trade-off is that a single missing criterion can drop a 100 to 80, but it's also unambiguous what to fix.

### Trace + state
After a run:
- **Trace** = ordered list of `{method, path, status, _clone, request_body?, response_body?}` for every API call the agent made.
- **State** = the twin's `_state` snapshot post-run (issues, channels, etc.).

Both are persisted in the run record, used by the evaluator, surfaced in the dashboard.

### Run record
A persisted JSON file per run at `.checkpoint/cache/runs/<id>.json`.  Contains everything: scenario, criteria results, trace, state, final answer, failure analysis (if generated), evaluator model, exit code, timestamp.  This is the single source of truth for the dashboard, `runs list`, `traces detail`, `compare`, `report`, and `debug usage`.

---

## 4. System architecture

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│  USER:  $ checkpoint run scenarios/multi-clone.md                                  │
└────────────────────────┬───────────────────────────────────────────────────────────┘
                         │
                         v
                  ┌──────────────┐                   ┌─────────────────┐
                  │   cli.py     │ ────reads──────►  │ .checkpoint.json│
                  │  (Click)     │                   │ harness.json    │
                  │              │                   │ ~/.checkpoint/  │
                  └──────┬───────┘                   └─────────────────┘
                         │
                  ┌──────v───────┐
                  │  scenario.py │  parses .md → Scenario(title, prompt, criteria, config)
                  └──────┬───────┘
                         │
                  ┌──────v───────┐                   ┌─────────────────┐
                  │  runner.py   │ ──spawn(uvicorn)► │ twins/github.py │
                  │              │ ──spawn(uvicorn)► │ twins/slack.py  │
                  │              │ ───POST /_seed──► │   ...           │
                  └──────┬───────┘                   └─────────────────┘
                         │  set CHECKPOINT_<CLONE>_URL env vars
                         │  + bootstrap tokens
                         v
                  ┌──────────────┐
                  │   harness    │  agent loop, makes HTTP calls to twins
                  │  (subproc)   │  prints final answer to stdout
                  └──────┬───────┘
                         │  exit code, stdout, stderr
                         v
                  ┌──────────────┐
                  │  runner.py   │  fetches each twin's /_state and /_trace
                  │  (continued) │
                  └──────┬───────┘
                         │
                  ┌──────v────────────────────────────────────────┐
                  │  3-stage evaluator                            │
                  │  checker.py     [D] regex/struct (Stage 1)    │
                  │  checker_llm.py [D] LLM-JSON (Stage 2)        │
                  │  judge.py       [P] GPT judge (Stage 3)       │
                  └──────┬────────────────────────────────────────┘
                         │
                  ┌──────v───────┐
                  │ run_record.py│  writes .checkpoint/cache/runs/<id>.json
                  └──────────────┘
                         │
                  ┌──────v───────┐
                  │  cli.py      │  prints score + criterion table; exit 0/1/2
                  └──────────────┘


              ┌───────────────────────────────────────────────────┐
              │  In a separate process: `checkpoint serve`        │
              │  ┌─────────────────────────────────────────────┐  │
              │  │  dashboard/app.py  (FastAPI + Jinja2-free)  │  │
              │  │  ├── /            ─► SPA (Vite/React build) │  │
              │  │  ├── /api/*       ─► JSON API               │  │
              │  │  ├── /api/events  ─► SSE event bus          │  │
              │  │  ├── /api/jobs    ─► spawn `checkpoint run` │  │
              │  │  ├── /api/docs    ─► Swagger UI             │  │
              │  │  ├── /healthz, /metrics                     │  │
              │  └─────────────────────────────────────────────┘  │
              └───────────────────────────────────────────────────┘
```

### Process model

In subprocess mode (default):
- **N+1 processes** for an N-clone scenario: 1 harness + N twin uvicorn workers.
- All on `127.0.0.1`, free ports.
- Lifetime = the `run_once` call.  `subprocess.terminate()` on exit (in `finally`).

In Docker mode:
- **N+2 containers**: 1 harness container + 1 sidecar container (mitmproxy) + N twin containers (which share the sidecar's network namespace via `network_mode=container:<sidecar>`).
- The harness's `extra_hosts` map every clone domain (`api.github.com`, `checkpoint.supabase.co`, etc.) to the sidecar's IP, so production URLs land at port 443 of the sidecar, which routes by Host header to the right twin port.

The dashboard runs in its own process via `checkpoint serve` and is independent of any run.  It watches the runs directory + clone registry and pushes changes to connected SSE clients.

---

## 5. The 3-stage evaluator

After the harness exits, every criterion in the scenario gets graded.  The pipeline is staged so we **only pay for an LLM call when a cheap deterministic check can't decide.**

### Stage 1 — Deterministic regex / structural matchers

`checkpoint/checker.py` defines a `PATTERNS` list of `(compiled_regex, matcher_fn)` tuples.  Each `matcher_fn` knows how to check a specific shape of criterion against the trace+state — e.g.:

- "Exactly N issue(s) exist" → count `state.issues`
- "An issue titled X exists" → search `state.issues` by title
- "Channel <foo> contains a message with X" → walk `state.messages`
- ... ~50 patterns covering the common phrasings

Stage 1 returns either a **definitive** `(passed, reasoning, "deterministic")` or a sentinel meaning "I don't recognize this criterion — pass to stage 2."

### Stage 2 — LLM-JSON checker

`checkpoint/checker_llm.py` is invoked when stage 1 punts.  It builds a tight prompt:

> Given this trace and state JSON, did the criterion hold?  Respond with `{"passed": bool, "reasoning": str}`.

Uses OpenAI's structured-output / JSON mode so the response is parseable.  ~1 cheap LLM call (`gpt-4o-mini` by default).  Returns `(passed, reasoning, "llm-json")`.

### Stage 3 — GPT judge

`checkpoint/judge.py` runs **only on `[P]` (perception) criteria** — those are explicitly marked as needing judgment beyond what state can prove.  The judge sees the final answer + final state and renders a verdict.  ~1 LLM call per `[P]` criterion.

### Failure analysis (post-evaluation, optional)

`checkpoint/failure_analysis.py` (and `failure_analyzer.py`) take the failed criteria + trace + state and ask an LLM "why did the agent fail to satisfy these?"  The output is stored in `record.failure_analysis` and surfaced in the dashboard's Run Detail page.  Skip with `--no-failure-analysis`.

### Why staged?

A scenario with 8 deterministic + 2 perception criteria costs ~3 LLM calls (2 judge + maybe 1 failure analysis) instead of 10.  This matters when running scenarios at CI scale.

---

## 6. Twin design

Each twin (`checkpoint/twins/<name>.py`) is a **single-file FastAPI app** with a uniform shape:

```
checkpoint/twins/github.py
├── STATE = {                       # in-memory, per-process
│     "repositories": [...],
│     "issues": [...],
│     "_config": {...},             # runtime knobs
│     "_counters": {...},           # request counts for rate limit
│   }
├── TRACE = []                      # every non-introspection request
├── @app.middleware("http") auth_and_limits_middleware
├── @app.middleware("http") trace_middleware
├── @app.get/post/... (REST surface)
├── /_health, /_state, /_trace, /_reset, /_seed/<name>, /_config
├── checkpoint/twins/<name>_seeds/  (named JSON seeds shipped alongside)
└── app.mount("/mcp", mcp_app)      # MCP server (see Section 7)
```

### Why one file per twin

- A junior eng can read one twin end-to-end in 30 minutes.
- Each twin owns its own state shape, error envelope, and authentication semantics.  No leaky base class.
- Easy to grep ("how does GitHub handle a 404 on `/repos/{owner}/{repo}/issues`?" → `grep "owner.*repo.*issues" checkpoint/twins/github.py`).

### Auth model

Each twin enforces a **bootstrap token**:
- `github`: `ghp_AaBbCc...` (looks like a real GitHub PAT)
- `slack`: `xoxb-...`
- `stripe`: `sk_live_...`
- `linear`, `supabase`, `discord`, `google-workspace`: each their own format

These are checked-into source code (`checkpoint/proxy/routes.py`) — they are not secrets.  They exist so that real SDKs (which insist on authenticating) get a wire-compatible response.  In Docker mode the sidecar's `addon.py` rewrites whatever `Authorization` header the agent sent to be the bootstrap token, so the agent doesn't need to know it.

### Service-shaped errors

Every twin returns errors in the *real* service's envelope shape:
- GitHub: `{"message": "...", "documentation_url": "...", "errors": [...]}`
- Slack: `{"ok": false, "error": "channel_not_found"}`
- Stripe: `{"error": {"type": "...", "code": "...", "message": "..."}}`

This is non-negotiable.  Real SDKs branch on these shapes; a wrong-shape error breaks the test before the agent's logic is even exercised.

### Runtime knobs (`/_config`)

Each twin's `/_config` accepts a small set of keys:
- `rate_limit: int` — return 429 after N requests.  Currently fully enforced by the github twin; others honor the field but enforcement is per-twin.
- `permissions_denied: bool` — return 403 on any mutating method.
- `read_only: bool` — same as permissions_denied but with a Checkpoint-flavored message; also enforced by the runner's pre/post state-snapshot diff so other twins are covered even if their middleware hasn't been updated.

These are how scenarios like `github-adversarial.md` reproduce "what does the agent do when GitHub starts returning 429s halfway through?"

---

## 7. MCP layer

Every twin mounts a **FastMCP** server at `/mcp/` exposing tool names that mirror the real vendor MCP servers.  Code lives in `checkpoint/mcp_servers/<clone>_mcp.py`.  A small `_shim.py` wraps the FastAPI twin's REST handlers so MCP tool calls funnel through the same business logic and end up in the same `STATE` and `TRACE`.

Why this matters: agents that use Model Context Protocol (Claude, increasingly OpenAI agents) can discover and call tools the same way they would against the real service's MCP server, but every call lands in the twin and is graded.

`checkpoint clone tools <id>` lists the tools a running twin exposes by JSON-RPC `tools/list`.  Verified end-to-end in `tests/test_mcp_*.py`.

---

## 8. Scenarios — format and lifecycle

### File format

```markdown
# Multi-clone product launch

## Setup
A team is launching a new product. The eng channel is busy with deploy chatter
and there's an active payment intent for a customer who needs a refund.

## Prompt
You are operating across the GitHub, Slack, and Stripe systems for "acme".
Coordinate the launch: open a tracking issue, post a launch announcement to
#engineering, and refund the pending payment intent for customer cust_123.

## Expected Behavior
Open a single GitHub issue. Post one Slack message. Issue exactly one refund.

## Success Criteria
- [D] Exactly 1 GitHub issue exists
- [D] At least 1 Slack message in `engineering` mentions "launch"
- [D] Exactly 1 Stripe refund exists
- [P] The agent's final answer summarizes all three actions with IDs

## Config
clones: github, slack, stripe
seed: github=small-project, slack=engineering-team, stripe=subscription-heavy
runs: 1
timeout: 120
tags: cross-system, launch
```

### Section semantics

- `# Title` — display name; appears in dashboard
- `## Setup` — prose describing starting state.  When `setup-seed: auto` (or `true`) is in `## Config`, the runner uses an LLM to derive a JSON seed.  Otherwise it's documentation only.
- `## Prompt` (or `## Task`) — what the agent is told to do
- `## Expected Behavior` — optional human-readable expectation; not graded directly, used by the failure-analysis prompt
- `## Success Criteria` — bullet list of `[D]` and `[P]` criteria
- `## Config` — key:value lines parsed into `scenario.config` dict (clones, seed, runs, timeout, tags, evaluator-model, setup-seed)

### Parser

`checkpoint/scenario.py` walks the markdown by `^##` headings and assigns content to known sections via `_SECTION_ALIASES`.  Returns a `Scenario` dataclass.  Tolerant of extra sections (ignored) and case variation in headings.

### Lifecycle inside `run_once`

1. `parse_file(path)` → `Scenario`
2. CLI overrides applied (`--clone`, `--runs`, `--timeout`, `--seed-file`, `--setup-file`, `--keep-state`)
3. `_resolve_clones` → list of twin names
4. For each clone: `_start_twin` → `_wait_healthy` → POST `/_seed/<name>` (or `/_seed/<file>` or LLM-derived)
5. Build env: `CHECKPOINT_<CLONE>_URL`, `<CLONE>_TOKEN`, `CHECKPOINT_TASK`, runtime knobs
6. `subprocess.run(harness_cmd)` with timeout
7. Fetch `/_state` and `/_trace` from each twin
8. Evaluate criteria (3-stage)
9. Return `RunResult`
10. `finally`: terminate all twin processes

### Validating scenarios

`checkpoint validate <scn.md>` parses the file and surfaces missing required sections, unknown criterion shapes, and suspicious config (unknown clone names, non-integer runs, etc.).  Used in CI to fast-fail bad scenarios before a 60-second run.

---

## 9. Run modes

### Docker mode (DEFAULT)

```
$ checkpoint run scenarios/foo.md --harness-dir my-agent/
```

This is the **production-fidelity** path and the default since v0.2.  Customers' agents call **real production URLs** — `https://api.github.com`, `https://*.supabase.co`, `https://api.linear.app`, etc.  Checkpoint TLS-intercepts those calls and routes them to local twins.

The CLI delegates to `checkpoint/docker/runner.py`, which:

1. Builds a sidecar container image that runs `mitmproxy` listening on port 443 in reverse-proxy mode.
2. Builds the harness image from `<dir>/Dockerfile` (or auto-generates one).
3. Creates a Docker network.
4. Starts the sidecar with `CHECKPOINT_ROUTES` env (JSON map of domain → twin URL).
5. Waits for the sidecar to mint its CA cert into a shared volume (`/archal-out/ca.crt`).
6. Starts each twin container with `network_mode=container:<sidecar>` so they share the sidecar's network namespace and listen on `127.0.0.1:<port>` inside it.
7. Starts the harness container on the bridge network with `extra_hosts: { "api.github.com": <sidecar_ip>, ... }`.  The harness's `entrypoint.sh` merges `/etc/ssl/certs/ca-certificates.crt` with `/archal-out/ca.crt` into a combined CA bundle so OpenAI calls trust real CAs *and* sidecar-intercepted calls trust the minted CA.
8. The harness uses **real SDKs** — PyGithub, supabase-py, etc. — pointed at production URLs.  Outbound HTTPS gets DNS-hijacked to the sidecar; mitmproxy's addon (`checkpoint/proxy/addon.py`) rewrites the request to the right twin and stamps in the bootstrap token.
9. Harness exits, runner reads `/_state` and `/_trace` from each twin via the sidecar's netns, evaluates, returns.

**When to use:** always, unless you have a specific reason not to.  Docker mode is the only path where the agent's code is identical to what ships to production — same SDKs, same TLS verification, same JSON shapes, same retry behavior, same network round-trip semantics.

### Subprocess mode (`--no-docker` opt-out)

```
$ checkpoint run scenarios/foo.md --no-docker --harness "python my_agent/harness.py"
```

Twins run as `uvicorn` subprocesses on `127.0.0.1:<free_port>`; the harness reads `CHECKPOINT_<CLONE>_URL` env vars and calls those URLs directly.  No TLS, no DNS hijacking, no Docker.  Faster startup (~200ms per twin) but **the agent is not exercising real SDKs against production URLs.**

**When to use:** unit-test-style scenarios where the agent code is checkpoint-aware (reads the env var directly), or quick local iteration when Docker isn't running.  Not representative of production behavior.

### How the modes share code

`run_once` (subprocess) and `docker_run_once` have the same return type (`RunResult`), the same evaluation pipeline, and produce identical run records.  The CLI's `run` command picks one based on `--docker / --no-docker`, but everything downstream — `_persist_run_record`, the evaluator, the dashboard — is mode-agnostic.

### Docker preflight

When Docker mode is on (the default), the CLI pings the daemon **before** spinning up twins or building images.  If unreachable, it exits 2 with a clear message and a hint to either start Docker or pass `--no-docker`.  The `CHECKPOINT_NO_DOCKER=1` env var forces fallback for environments where Docker is intentionally unavailable (nested CI runners, etc.).

---

## 10. CLI surface

37 commands and subcommands.  All implemented via Click in `checkpoint/cli.py`.  Highlights:

### Core run loop

| Command | Purpose |
|---|---|
| `checkpoint run <scn>` | run a scenario, print score |
| `checkpoint run <dir/> --tag X -n 3 --pass-threshold 80` | CI mode |
| `checkpoint run <scn> -o json -q` | machine-readable output |
| `checkpoint run <scn> --docker --harness-dir harness/` | docker mode |
| `checkpoint run <scn> --read-only` | snapshot diff; fail if state changed |
| `checkpoint run <scn> --rate-limit 50` | cap requests per twin |
| `checkpoint run <scn> --keep-state` | reuse twin state from prior run |
| `checkpoint run <scn> --seed-file ./s.json --setup-file ./s.txt` | CLI overrides |

### Inspection / history

| Command | Purpose |
|---|---|
| `checkpoint runs list` | recent run records |
| `checkpoint traces detail [id]` | pretty-print a run record |
| `checkpoint replay [id]` | walk through API calls of a past run |
| `checkpoint compare <a> <b>` | criterion-level diff between runs |
| `checkpoint report [pattern]` | trend + flaky detection |
| `checkpoint debug usage` | aggregate counts, scores, LLM/tool calls |
| `checkpoint debug export <id> -o out.json --anonymize` | sanitize PII for sharing |
| `checkpoint debug inspect [id]` | alias for `traces detail` |

### Twin lifecycle

| Command | Purpose |
|---|---|
| `checkpoint clone start <id> [--ttl-seconds N --seed NAME]` | start a long-lived twin |
| `checkpoint clone list / status <id>` | enumerate / inspect |
| `checkpoint clone seed <id> <name>` | reseed a running twin |
| `checkpoint clone reset <id>` | factory reset state |
| `checkpoint clone tools <id>` | list MCP tools |
| `checkpoint clone renew <id> --ttl-seconds N` | extend TTL |
| `checkpoint clone stop <id>` | stop |

### Scenario authoring

| Command | Purpose |
|---|---|
| `checkpoint scenario list` | enumerate `.md` scenarios under cwd |
| `checkpoint scenario generate <description>` | LLM-author a starter scenario |
| `checkpoint scenario coverage` | which criteria are stage-1 hits, which need stage-2/3 |
| `checkpoint validate <scn.md>` | parse + lint |

### Identity / config

| Command | Purpose |
|---|---|
| `checkpoint whoami` | local identity (version, paths, judge model, OPENAI_API_KEY presence) |
| `checkpoint config init / show / get / set / unset / path` | manage `~/.checkpoint/config.json` |
| `checkpoint doctor` | environment readiness check (Docker, ports, key) |

### Project lifecycle

| Command | Purpose |
|---|---|
| `checkpoint init [target]` | scaffold `.checkpoint.json`, harness, starter scenario |
| `checkpoint serve` | start the web dashboard |
| `checkpoint docker build` | build a harness image without running |
| `checkpoint badge <run_id>` | generate a shields.io badge URL |
| `checkpoint ci` | CI integration helpers |

### Exit codes

- `0` — all scenarios passed (or `--pass-threshold` met)
- `1` — at least one scenario failed (or threshold violated)
- `2` — config / argument error (missing harness, scenario file not found, etc.)

### Config precedence

Everywhere the CLI reads a value:

```
explicit flag  >  project .checkpoint.json  >  user ~/.checkpoint/config.json  >  env  >  built-in default
```

`~/.checkpoint/config.json` supports `env:NAME` indirection (`"openai_api_key": "env:OPENAI_API_KEY"`) so secrets stay out of the file.

---

## 11. The dashboard

Local web UI at `http://127.0.0.1:4001`.  Started by `checkpoint serve`.  Two layers:

### Backend — `checkpoint/dashboard/`

| File | Role |
|---|---|
| `app.py` | FastAPI app factory, all routes, middleware wiring |
| `events.py` | `EventBus` (in-process pub/sub) + `FilesystemWatcher` (polls runs dir + clone registry, publishes events) |
| `jobs.py` | `JobManager` — spawns `checkpoint run` as background tasks, captures stdout, broadcasts to SSE listeners |
| `middleware.py` | `RequestIdMiddleware`, `AccessLogMiddleware`, `RateLimitMiddleware`, `BearerAuthMiddleware`, `ReadOnlyJobsMiddleware` |
| `metrics.py` | Prometheus exposition format (`/metrics`) — uptime, http counts/duration, jobs, SSE subscribers |

**Cloud-deploy hardening (env-gated, all opt-in):**

- `CHECKPOINT_DASHBOARD_API_KEY=<token>` — `BearerAuthMiddleware` requires `Authorization: Bearer <token>` on every `/api/*` write.  Reads stay open unless `CHECKPOINT_DASHBOARD_AUTH_READS=1`.  Always-public: `/healthz`, `/metrics`, `/api/docs`, `/api/openapi.json`, SPA static.
- `CHECKPOINT_DASHBOARD_READ_ONLY=1` — `ReadOnlyJobsMiddleware` returns 403 on `POST/PUT/DELETE /api/jobs`.  Use for viewer-only deployments.

These middlewares are no-ops when their env vars are unset, so the local-dev UX stays one-command (`checkpoint serve`).

**Key endpoints:**

| Path | Purpose |
|---|---|
| `GET /` | SPA index.html |
| `GET /assets/*` | Vite-built JS/CSS chunks |
| `GET /healthz` | liveness |
| `GET /metrics` | Prometheus exposition |
| `GET /api/docs` | Swagger UI (auto-generated from FastAPI route signatures) |
| `GET /api/openapi.json` | OpenAPI spec |
| `GET /api/meta` | version, host, runs_dir, scenarios_dir, judge_model_default |
| `GET /api/runs?scenario=&page=&per_page=` | paginated summary list |
| `GET /api/runs/{id}` | full record (criteria + trace + state) |
| `GET /api/summary` | total runs, avg/pass-rate over last 30d |
| `GET /api/scenarios?path=` | scenarios + coverage |
| `GET /api/report?scenario=&limit=` | trend + flaky |
| `GET /api/compare?a=&b=` | rec_a + rec_b + diff |
| `GET /api/clones` | live clone registry |
| `POST /api/jobs` | start a `checkpoint run` job |
| `GET /api/jobs / {id}` | list / get |
| `DELETE /api/jobs/{id}` | cancel |
| `GET /api/jobs/{id}/stream` | SSE: live stdout + ended event |
| `GET /api/events` | SSE: `run.created`, `run.updated`, `clones.changed`, `job.updated` |

`StartJobBody` uses Pydantic `extra="forbid"` so unknown fields (like a hypothetical `extra_args`) are rejected with HTTP 422.  This closes a flag-injection vector when the dashboard is bound to `0.0.0.0`.

### Frontend — `checkpoint/dashboard/web/` (source) and `checkpoint/dashboard/static/` (built)

Vite + React 18 + TypeScript + Tailwind 3.  Design tokens (paper/ink/accent palette, sharp corners, offset shadows, Geist font, animated blip) lifted from `usecheckpoint.dev`.

**Pages:**

| Route | Component | What it does |
|---|---|---|
| `/` | `Runs` | sortable run list, summary tiles, live clones, scenario launcher |
| `/runs/:id` | `RunDetail` | criteria table, failure analysis, deep trace inspector, JSON download |
| `/scenarios` | `Scenarios` | bundled + local scenarios with stage-1 coverage |
| `/report` | `Report` | sparkline + per-criterion pass-rate + flaky badges |
| `/compare?a=&b=` | `Compare` | side-by-side diff with regressions/fixes/added/removed |
| `/live/:jobId` | `LiveRun` | SSE-streamed stdout/stderr from a running job |

**Key components:**

- `Layout` — sticky nav + util bar + dark mode toggle + global SSE event subscriber that invalidates TanStack Query caches on `run.created`, `clones.changed`, etc.
- `CommandPalette` — ⌘K, fuzzy search across pages/runs/scenarios
- `CompareBar` — floating "you've picked 2 runs, click to compare"
- `TraceTimeline` — per-event request/response inspection with copy-as-curl, filter by clone/method/status
- `bits.tsx` — shared `Badge`, `ScoreBar`, `StatTile`, `Sparkline`, `EmptyState`, `Loading`, `ErrorBox`

**State management:** TanStack Query for server state.  Tiny custom `createStore` for UI state (compare picks, dark mode).  React Router v6 for routing.  No Redux / no Zustand.

**SSE wiring:** `useEventSource` hook in `lib/sse.ts` — auto-reconnect with exponential backoff, named-event listeners.  `useJobStream` builds on it for live log viewer.

### Build and ship

`npm run build` writes to `../static/`.  That directory is **committed to git** (the JupyterLab pattern), so:
- `pip install checkpoint` from PyPI just works — no Node required.
- CI runs `npm run build` on every PR and `git diff --exit-code -- checkpoint/dashboard/static/` — if a contributor changes the SPA but forgets to commit a fresh build, the PR fails.

Bundle size: 246 KB total, 79 KB gzipped (4 chunks: react, query, app, css).

---

## 12. Distribution and packaging

### What ships in the wheel

`pyproject.toml` declares as `[tool.setuptools.package-data]`:

- `checkpoint/init_templates/**/*` — files copied by `checkpoint init`
- `checkpoint/dashboard/static/**/*` — the built SPA bundle
- `MANIFEST.in` ensures sdists carry the same trees on older setuptools

### What does NOT ship

- `checkpoint/dashboard/web/` (SPA *source*) — only the build output ships
- `node_modules/`, `*.tsbuildinfo`, generated `vite.config.{d.ts,js}` — `.gitignore`d
- `.checkpoint/cache/` — runtime data
- Test files

### Dependencies (runtime)

```
fastapi>=0.110     # twins + dashboard
uvicorn[standard]  # twin + dashboard server
httpx>=0.27        # twin <-> runner, runner -> twins
click>=8.1         # CLI
rich>=13.7         # CLI tables/panels
openai>=1.30       # judge, checker_llm, scenario_gen
python-dotenv      # .env loading
mitmproxy>=10      # Docker mode TLS interception
docker>=7          # Docker mode container orchestration
mcp>=1.27          # MCP server framework
sse-starlette>=2.1 # dashboard SSE
```

Dev extras: `pytest>=8.0`, `pytest-asyncio>=0.23`.

### Versioning

`__version__` lives in `checkpoint/__init__.py` and is referenced by `pyproject.toml` (currently `0.1.0`) and the FastAPI `version` field on the dashboard's OpenAPI doc.  Bump both when releasing.

---

## 13. Testing strategy

**46 test files, 800+ tests.**  Each test file is scoped to a single module or feature area.

### Categories

| Category | Files | What they cover |
|---|---|---|
| Twin tests | `test_supabase_twin.py`, `test_routes.py`, etc. | REST surface fidelity, error envelopes, seeds |
| MCP tests | `test_mcp_*.py` (7 files) | every twin's MCP tool surface — list_tools, call_tool, transport |
| CLI tests | `test_cli_*.py`, `test_cli_new_commands.py` | command parsing, exit codes, output formats |
| Evaluator | `test_checker_*.py`, `test_judge.py`, `test_failure_analysis.py` | regex catalog, LLM-JSON, judge logic |
| Runner | `test_runs_analytics.py`, `test_run_runtime_flags.py` | analytics, --rate-limit/--read-only/--keep-state |
| Dashboard | `test_dashboard.py` | every JSON route, SPA fallback, rate limit, request IDs, jobs lifecycle, SSE bus |
| Sandbox/Docker | `tests/docker/` | harness image build, runner unit tests |
| Phase acceptance | `test_phase{5,6,7,8}_*` | end-to-end smoke tests for each major phase |
| User config | `test_user_config.py` | env indirection, prune, coercion |
| Scenarios | `test_scenario_authoring.py`, `test_phase8_scenario_library.py` | parser, validate, bundled library invariants |

### Patterns

- **Fixtures** isolate `~/.checkpoint` and runs dir into `tmp_path` via `CHECKPOINT_HOME` env override, so tests never touch the developer's machine.
- **Mocked LLM calls** — judge / checker_llm / failure_analyzer tests use `monkeypatch` on the OpenAI client, so the suite runs offline.
- **Real twins** — twin tests spin up the actual FastAPI app with `httpx.Client(app=app)` for in-process testing — no port allocation needed.
- **CLI tests** use `click.testing.CliRunner` for fast in-process invocation.
- **SSE tests** unit-test the `EventBus` directly rather than streaming via TestClient (Windows asyncio + sync streaming deadlocks).

### What's NOT covered

- Real OpenAI calls in CI — gated behind `OPENAI_API_KEY` secret which the eval CI job skips cleanly when missing.  Local devs with the key run scenarios end-to-end.
- Real Docker mode in pytest — tests/docker/ verifies image *generation*, not actual container runs.  Manual smoke is the gate (the v0.1.0 multi-clone Docker test scored 100/100 before release).

---

## 14. CI/CD

`.github/workflows/checkpoint-ci.yml` has **two jobs**, both on `ubuntu-latest`:

### Job 1: `eval` — agent-eval scenario run

1. Install Python 3.11 + checkpoint
2. Detect `OPENAI_API_KEY` secret; skip the rest cleanly if missing (warns, exits 0)
3. `checkpoint doctor`
4. `checkpoint run scenarios/` against a default harness
5. Upload trace artifact + run records

### Job 2: `build-and-test` — gates every PR

1. setup-node 20 (with npm cache pinned to `package-lock.json`)
2. setup-python 3.11
3. `cd checkpoint/dashboard/web && npm ci && npm run build`
4. `git diff --exit-code -- checkpoint/dashboard/static/` — fails if SPA bundle drifted from a fresh build
5. `pip install -e ".[dev]"`
6. `pytest tests/ -q` (~834 tests, ~100s on CI)
7. `python -m build` (sdist + wheel)
8. `twine check dist/*`
9. Verify `dashboard/static/index.html` is inside the built wheel via Python one-liner
10. Upload `dist/*` as a build artifact

Both jobs run in parallel.  The `build-and-test` job is the real gate — it's pure Python+JS, no secrets needed, runs on every fork PR.  The `eval` job is the integration smoke — needs the secret to actually exercise the LLM evaluator.

### Release flow

There's no automated PyPI publish today.  When ready to ship:
1. Bump `__version__` and `pyproject.toml` `version`
2. Push to main → CI green
3. `git tag v0.1.0 && git push --tags`
4. Manually `python -m build && twine upload dist/*`

---

## 15. Security model

Checkpoint is **local-first by default, deploy-anywhere by design.**  There is no Checkpoint-operated remote backend, no accounts in our database, no telemetry.  But the same image that runs locally can be deployed to the customer's own infrastructure (Fly, Render, k8s, EC2, bare metal — see [DEPLOYMENT.md](DEPLOYMENT.md)) and hardened with bearer-token auth + read-only mode.

This is a deliberate product choice: the surface area for "Checkpoint *as a vendor* got hacked" is zero because we don't store customer data.  Customers who want a shared dashboard self-host one in 5 minutes; the entire stack ships as a single Docker image with `Dockerfile` / `docker-compose.yml` / `fly.toml` / `render.yaml` at the repo root.

The trade-offs we accept:
- We don't compete with hosted observability platforms — that's their lane.
- "Share this run with your team via URL" requires the team to deploy the dashboard.
- We don't maintain auth/billing/multi-tenant infra — small team focus.

### Threats considered and addressed

| Threat | Mitigation |
|---|---|
| **Token leaking in run records shared via screenshot** | `checkpoint debug export <id> --anonymize` regex-redacts emails, GitHub PATs, OpenAI keys before writing |
| **Dashboard `--host 0.0.0.0` exposes job-launcher to LAN** | `POST /api/jobs` schema is `extra="forbid"` — only typed fields accepted; no flag injection.  Rate limit (30 writes/10s/IP) on `/api/*`.  When `CHECKPOINT_DASHBOARD_API_KEY` is set, all writes need a bearer token.  When `CHECKPOINT_DASHBOARD_READ_ONLY=1` is set, the jobs endpoint returns 403 entirely.  README + DEPLOYMENT.md document the LAN-exposure risk explicitly. |
| **Cloud deploy where the dashboard is publicly reachable** | Set both env vars above; terminate TLS at your reverse proxy; restrict ingress to a VPN/corp IP range.  Dashboard image runs as non-root UID 10001 with `tini` as PID 1 and a `/healthz` for load balancers. |
| **Twin bootstrap tokens in source code** | They are intentionally in source — they grant access only to *the twin*, which has nothing real behind it.  Not credentials. |
| **Stale clone subprocesses orphaned by a crashed CLI** | `clone start` uses `start_new_session=True`; `clone list` auto-purges entries whose PIDs are gone. |
| **CSRF / XSS on the dashboard** | Same-origin only; no cookies, no auth state.  React escapes all rendered text by default.  No `dangerouslySetInnerHTML` anywhere. |
| **Secrets in `.checkpoint.json` that gets committed** | `.checkpoint.json` is project config and may be committed; secrets belong in env vars or `~/.checkpoint/config.json` with `env:NAME` indirection.  Documented in README. |
| **Docker harness escaping the sidecar** | The harness container runs as the user, no `--privileged`, only sees the bridge network + the sidecar's IP.  CA cert is per-run and removed on teardown. |

### Out of scope (deliberately)

- Multi-tenant isolation (it's a local tool — one user per process)
- Audit log of who did what (no auth, no users)
- Encryption of run records at rest (they live in `.checkpoint/cache/runs/` on the user's machine; standard FS permissions)

---

## 16. Repository tour

```
checkpoint/
├── __init__.py                  # __version__
├── cli.py                       # 1700 LOC — every CLI command
├── runner.py                    # subprocess-mode run_once
├── scenario.py                  # parse markdown -> Scenario
├── checker.py                   # Stage 1 deterministic patterns
├── checker_llm.py               # Stage 2 LLM-JSON
├── judge.py                     # Stage 3 GPT judge
├── compare_diff.py              # pure function: diff two run records
├── analytics.py                 # compute_trend, detect_flaky, load_runs_for_scenario
├── failure_analysis.py          # heuristic failure analysis
├── failure_analyzer.py          # LLM-driven failure analysis
├── config.py                    # .checkpoint.json + harness.json discovery
├── user_config.py               # ~/.checkpoint/config.json (CRUD + env indirection)
├── identity.py                  # whoami snapshot
├── clone_manager.py             # checkpoint clone start/list/seed/reset/tools/...
├── run_record.py                # build_record, write_record, RUNS_DIR
├── pytest_plugin.py             # pytest fixtures for downstream test suites
├── sdk.py                       # programmatic Python SDK
├── init.py                      # checkpoint init scaffolding
├── scenario_gen.py              # LLM-author a scenario
├── scenario_seed_gen.py         # LLM-derive a JSON seed from prose
├── diagnostics.py               # checkpoint doctor checks
├──
├── twins/
│   ├── github.py + github_seeds/
│   ├── slack.py + slack_seeds/
│   ├── stripe.py + stripe_seeds/
│   ├── linear.py + linear_seeds/
│   ├── supabase.py + supabase_seeds/
│   ├── discord.py + discord_seeds/
│   └── google_workspace.py + google_workspace_seeds/
├──
├── mcp_servers/
│   ├── _shim.py                 # adapter: MCP tool call -> twin REST handler
│   └── <clone>_mcp.py × 7
├──
├── proxy/                       # Docker-mode TLS sidecar
│   ├── Dockerfile
│   ├── entrypoint.sh
│   ├── addon.py                 # mitmproxy addon: route by Host header, swap auth
│   ├── routes.py                # domain -> (twin URL, bootstrap token) registry
│   └── ca.py                    # mint per-run CA cert
├──
├── docker/                      # Docker-mode runner
│   ├── runner.py                # docker_run_once orchestration
│   └── harness_image.py         # build harness image from user dir
├──
├── sandbox/                     # pre-baked harness sandbox image
│   └── Dockerfile
├──
├── dashboard/
│   ├── app.py                   # FastAPI factory + all routes
│   ├── events.py                # EventBus + FilesystemWatcher
│   ├── jobs.py                  # JobManager (spawn checkpoint run)
│   ├── middleware.py            # request ID, access log, rate limit
│   ├── metrics.py               # Prometheus exposition
│   ├── static/                  # built SPA (committed)
│   └── web/                     # SPA source: vite + react + ts + tailwind
│
└── init_templates/              # files checkpoint init copies into the user's repo

scenarios/                       # 16 bundled scenarios
checkpoint-vitest/               # @checkpoint/vitest npm package
tests/                           # 46 test files
.github/workflows/checkpoint-ci.yml
.checkpoint/cache/runs/<id>.json # runtime run records (gitignored)
~/.checkpoint/config.json        # user-level config (per machine, not in repo)
README.md                        # quickstart + CLI table + dashboard docs
DESIGN.md                        # this file
```

---

## 17. Extension points

### Adding a new twin

1. Create `checkpoint/twins/<name>.py` — single FastAPI app, in-memory state, the standard `/_health /_state /_trace /_reset /_seed/<n> /_config` introspection, one or more REST routes, a per-twin auth middleware that checks the bootstrap token.
2. Create `checkpoint/twins/<name>_seeds/{small-project,empty,...}.json` for named seeds.
3. Register in `checkpoint/runner.py:TWIN_APPS` and `checkpoint/clone_manager.py:TWIN_APPS`.
4. Add a bootstrap token in `checkpoint/proxy/routes.py:_ROUTES` and `checkpoint/runner.py:_CLONE_BOOTSTRAP_TOKEN_ENV`.
5. Add `_CLONE_DOMAINS["<name>"]` in `checkpoint/docker/runner.py` for the production domains the SDK calls.
6. Optionally add `checkpoint/mcp_servers/<name>_mcp.py` to expose an MCP surface.
7. Write `tests/test_<name>_twin.py` and `tests/test_mcp_<name>.py`.
8. Add scenarios under `scenarios/`.

### Adding a new criterion pattern

Add to `checkpoint/checker.py:PATTERNS`:

```python
PATTERNS.append((
    re.compile(r"^Channel (\w+) has (?:exactly\s+)?(\d+) members?$", re.I),
    lambda m, trace, state: _check_channel_members(state, m.group(1), int(m.group(2))),
))
```

If your matcher returns `None`, the criterion falls through to stage 2.

### Adding a new CLI command

In `checkpoint/cli.py`:

```python
@main.command("export-html")
@click.argument("run_id", required=False)
def export_html(run_id):
    """Export a run record as standalone HTML."""
    record = _load_run_record(run_id)
    ...
```

If it's a subcommand of an existing group, use `@traces.command(...)` instead.

### Adding a new dashboard page

1. Add the route under `src/App.tsx`.
2. Create `src/pages/MyPage.tsx`.
3. Add a JSON endpoint in `checkpoint/dashboard/app.py` if needed.
4. Add a `useQuery` hook in your page that hits the endpoint via `api.myThing()`.
5. Add types + the `api.myThing` wrapper in `src/lib/api.ts`.
6. `npm run build` → commit `static/`.
7. Add a test in `tests/test_dashboard.py`.

### Adding a runtime knob to all twins

1. Update `checkpoint/twins/<each>.py:set_config` to accept the new key.
2. Add the gate to that twin's middleware.
3. Add a `--<knob>` flag to `checkpoint run` in `cli.py`.
4. Surface it as an env var (`CHECKPOINT_RUNTIME_<KNOB>`) so `runner.py` POSTs `/_config` to each twin after startup.
5. Document under `clone_manager.configure(...)`.

---

## 18. Glossary

- **Twin** — stateful FastAPI app imitating one SaaS service.
- **Clone** — user-facing name for a running twin instance.  In docs sometimes the same as "twin"; in `clone_manager.py` it's specifically a long-lived registered session.
- **Scenario** — markdown file with prompt + criteria + config.
- **Harness** — user's agent script, spawned by the runner.
- **Seed** — starting state loaded into a twin before the run.
- **Criterion** — one pass/fail check.  `[D]` deterministic, `[P]` perception.
- **Stage 1/2/3** — the three evaluator stages: regex → LLM-JSON → GPT judge.
- **Run record** — the JSON file persisted to `.checkpoint/cache/runs/<id>.json` after each run.
- **Bootstrap token** — fixed token each twin accepts; not a secret.
- **Sidecar** — the mitmproxy container in Docker mode.
- **Route mode** — synonym for the sidecar's TLS-interception behavior.
- **MCP** — Model Context Protocol; every twin mounts an MCP server at `/mcp/`.
- **`.checkpoint.json`** — *project*-level config (in repo, committed).
- **`~/.checkpoint/config.json`** — *user*-level config (per-machine, not in repo).
- **`harness.json`** — manifest sitting next to a harness; declares command/env/dockerfile.
- **Pass-threshold** — minimum avg satisfaction score for `checkpoint run` to exit 0.
- **Read-only mode** — runner snapshots state pre/post and fails the run if anything changed; useful for "this agent is supposed to investigate, not modify."

---

## Appendix A: a representative trace through `checkpoint run`

For a scenario `scenarios/github-supabase-product-launch.md` with `clones: github, supabase`:

```
1.  cli.run() parses the .md via scenario.py → Scenario(clones=['github','supabase'], ...)
2.  cli.run() applies CLI overrides (--seed-file, --keep-state, etc.)
3.  cli.run() resolves the harness (--harness > harness.json > .checkpoint.json)
4.  cli.run() resolves the judge model (--model > scenario.config > .checkpoint.json > env > "gpt-4o-mini")
5.  cli.run() calls runner.run_once(scenario, harness_cmd, ...)
6.  runner._free_port() × 2  → 54321, 54322
7.  runner._start_twin('github', 54321)   → uvicorn subprocess on :54321
    runner._start_twin('supabase', 54322) → uvicorn subprocess on :54322
8.  runner._wait_healthy(54321), _wait_healthy(54322)  → both /_health return 200
9.  runner._apply_named_seed(54321, 'small-project') → POST /_seed/small-project to github twin
    runner._apply_named_seed(54322, 'ecommerce')     → POST /_seed/ecommerce to supabase twin
10. If --rate-limit / --read-only set: POST /_config to each twin
11. If --read-only: GET /_state from each twin, store as pre-snapshot
12. Build env: CHECKPOINT_GITHUB_URL=http://127.0.0.1:54321,
              CHECKPOINT_SUPABASE_URL=http://127.0.0.1:54322,
              GITHUB_TOKEN=ghp_AaBbCc...,
              SUPABASE_BOOTSTRAP_TOKEN=eyJh...,
              CHECKPOINT_TASK="A new product is ready to ship: ..."
13. subprocess.run(['python', 'harness/harness.py'], env=env, timeout=120)
    → harness loops: OpenAI tool-calling against twins, ~5-10 round trips
    → harness prints {"text": "Created issue #4 and inserted product abc-123"}
14. runner reads /_state and /_trace from each twin via httpx
15. If --read-only: GET /_state again, diff against pre-snapshot.
                    If different → append a failed CriterionResult, continue.
16. _evaluate(scenario, result, judge_model):
      For each criterion:
        Stage 1 → checker.check(criterion, trace, state)
                  Returns (passed, reasoning, "deterministic") OR sentinel
        If sentinel: Stage 2 → checker_llm.try_stage2(...)
                                Returns (passed, reasoning, "llm-json")
        If kind == "P":  Stage 3 → judge.judge(criterion, final_answer, state, ...)
                                    Returns (passed, reasoning, "llm")
17. cli._print_run() renders rich table to stdout
18. cli._persist_run_record():
      - if scenario failed AND not --no-failure-analysis:
          failure_analyzer.analyze_failures(...) → ~1 LLM call → record.failure_analysis
      - run_record.write_record() → .checkpoint/cache/runs/<id>.json
19. runner finally: terminate both twin subprocesses
20. cli exits 0/1/2 based on pass-threshold or all-100 default
21. Dashboard's FilesystemWatcher (if running) detects the new file → publishes
    "run.created" → SSE clients (the open browser tab) get it → TanStack Query
    invalidates the runs cache → table refreshes without a page reload.
```

---

## Appendix B: where to look first when debugging

| Symptom | Look here first |
|---|---|
| Scenario hangs forever | `runner._wait_healthy` (twin didn't start) or harness `subprocess.run(timeout=...)` (your agent looped) |
| Score is 0/100 | `record.criteria` — usually means harness exited non-zero or scenario has no criteria |
| Wrong twin called | `_CLONE_DOMAINS` in `docker/runner.py` (Docker mode) or `CHECKPOINT_<CLONE>_URL` env (subprocess mode) |
| 401 from twin in Docker mode | `proxy/addon.py:RouteMode.request` rewrites Authorization; check if it ran |
| Dashboard 503 at `/` | `dashboard/static/` missing — `cd dashboard/web && npm run build` |
| `--rate-limit` not enforced | Twin doesn't honor it yet; only github does today.  `clone_manager.configure(...)` is the API |
| MCP `tools/list` returns empty | `mcp_servers/<clone>_mcp.py` not wired, or twin's `app.mount("/mcp", ...)` missing |
| Test that asserts on rich text fails on Linux but passes on Windows | Rich wraps at narrow terminal width; collapse whitespace before substring check |

---

*Last updated: 2026-05-15 (v0.1.0 + v0.2 CLI sweep).  Maintainer: this doc lives at the repo root; keep it current as the codebase evolves.*
