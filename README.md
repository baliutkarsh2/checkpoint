# checkpoint

**Test your AI agent against stateful synthetic GitHub / Slack / Stripe / Linear / Supabase / Discord / Google Workspace. Score 0-100 with deterministic + LLM checks. No real-API credits burned.**

Your agent calls **`https://api.github.com`** unmodified.  Checkpoint TLS-intercepts the call and routes it to a local twin that responds wire-compatibly.  Same code path you ship to production, evaluated against deterministic + LLM-judged success criteria.

## Supported twins

GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace.  Each twin is a full FastAPI app with REST + MCP tool surface, named seeds, runtime knobs (rate-limit / read-only / permissions-denied), and introspection at `/_health`, `/_state`, `/_trace`, `/_reset`, `/_seed/<name>`, `/_config`.

## Install

```bash
pip install checkpoint
export OPENAI_API_KEY=sk-...
```

You also need **Docker running** — that's the default run mode (real SDKs against production URLs).

## Quickstart — score one of the bundled agents

```bash
# 4 example agents under examples/agents/. Pick the one matching your stack.
checkpoint run scenarios/github-happy-path.md \
    --harness-dir examples/agents/openai-tools \
    --docker-logs
```

You'll see the agent's stderr stream live, then a scored criterion table.  The 4 example agents:

| Agent | Stack |
|---|---|
| `examples/agents/openai-tools/` | OpenAI function-calling + PyGithub + supabase-py + slack_sdk |
| `examples/agents/anthropic-tools/` | Anthropic SDK tool-use + same SDKs |
| `examples/agents/langchain-react/` | LangChain `create_react_agent` |
| `examples/agents/mcp-client/` | Pure MCP client hitting the twin's `/mcp/` |

See [`examples/agents/README.md`](examples/agents/README.md) for the full guide.

## Quickstart — your own agent

```bash
checkpoint init                                   # scaffolds harness/, .checkpoint.json, scenario
checkpoint run scenarios/                         # run all (Docker mode is default)
checkpoint run scenarios/ -n 3 --pass-threshold 80 -o json -q   # CI-friendly
checkpoint run scenarios/foo.md --no-docker       # subprocess mode (fast, no real SDKs)
```

## Dashboard

```bash
checkpoint serve         # http://127.0.0.1:4001
```

Local web UI for browsing run history, comparing runs, watching live clones, and launching new runs from the browser.  Single-page React app served by the same FastAPI process that exposes the JSON API.

What you get:

- **Runs browser** — score sparklines, criterion-level pass/fail, deep trace inspector with per-event request/response bodies and "copy as curl"
- **Run launcher** — pick a scenario, hit start, watch stdout/stderr stream in real time over Server-Sent Events
- **Live clone panel** — auto-updates as `checkpoint clone start` adds/removes twins
- **Compare flow** — tick two runs in the table, click Compare for a side-by-side diff
- **Trend report** — per-scenario sparkline + per-criterion pass-rate table + flaky detection
- **OpenAPI Swagger UI** at `/api/docs` — every endpoint typed and try-able
- **Prometheus metrics** at `/metrics` — request counts, durations, jobs, SSE subscribers
- **Command palette** (`⌘K`), keyboard nav (`g r`, `g s`, `g p`), dark mode (`d`)

## Deploying the dashboard

For a team-shared dashboard or CI integration:

```bash
docker compose up -d                       # self-host on any Linux box
flyctl launch && flyctl deploy             # Fly.io one-command deploy
```

Full guide: [DEPLOYMENT.md](DEPLOYMENT.md) — Docker, docker-compose, Fly.io, Render, Kubernetes, bare wheel + systemd.  All cloud paths use the same multi-stage `Dockerfile` at the repo root, with bearer-token auth, optional read-only mode, persistent volume for runs, and a `/healthz` for load balancers.

## Mental model

A **twin** is a stateful synthetic SaaS API running locally.  A **scenario** is a markdown file with `## Setup`, `## Prompt`, and `## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged).  A **harness** is your agent in a Docker image with a `Dockerfile` + `harness.py` + `entrypoint.sh`.  Checkpoint spins up the twins, the TLS sidecar, runs your harness, grades criteria, returns a score.

```
checkpoint run scenarios/foo.md --harness-dir my-agent/
    |-- spins up sidecar (mitmproxy on :443) + N twin containers in shared netns
    |-- starts your harness container with extra_hosts mapping production
    |   domains (api.github.com, *.supabase.co, ...) to the sidecar
    |-- harness uses real SDKs against production URLs; sidecar routes them
    |-- collects trace + state from each twin
    `-- 3-stage eval: regex patterns -> LLM-JSON schema -> GPT judge
```

Pass `--no-docker` for fast in-process subprocess mode (your harness reads `CHECKPOINT_<CLONE>_URL` env vars and calls those directly — useful for unit-test-style scenarios where you don't need real SDK fidelity).

## CLI reference

| Command | Purpose |
|---|---|
| `checkpoint init` | Scaffold integration in the current repo |
| `checkpoint serve` | Start the web dashboard at http://127.0.0.1:4001 |
| `checkpoint whoami` | Print local identity (version, paths, judge model, OPENAI key) |
| `checkpoint config show \| set \| get \| unset \| init \| path` | Manage `~/.checkpoint/config.json` |
| `checkpoint run <scenario.md>` | Run a scenario, print score (Docker mode is default) |
| `checkpoint run <dir/> --tag smoke -n 3 --pass-threshold 80` | CI mode: 3 runs, fail if any avg < 80 |
| `checkpoint run <scn> -o json -q --no-failure-analysis` | Machine-readable summary, no LLM-driven failure analysis |
| `checkpoint run <scn> --no-docker` | Subprocess mode (fast iteration; no real SDKs) |
| `checkpoint run <scn> --read-only` | Snapshot twin state pre/post; fail if agent wrote anything |
| `checkpoint run <scn> --rate-limit 50` | Cap requests per twin (currently enforced by github twin) |
| `checkpoint run <scn> --keep-state` | Don't reseed — start from previous run's state |
| `checkpoint run <scn> --seed-file ./seed.json --setup-file ./setup.txt` | Override scenario seed/setup at the CLI |
| `checkpoint validate <scenario.md>` | Parse and lint a scenario file |
| `checkpoint replay [run_id]` | Replay API trace from a past run |
| `checkpoint doctor` | Verify environment (Docker, ports, API key) |
| `checkpoint scenario list` | Enumerate scenarios in cwd |
| `checkpoint clone start <id> [--ttl-seconds N --seed NAME]` | Spin up a long-lived twin session |
| `checkpoint clone list` / `clone status <id>` | List all running twins / show one |
| `checkpoint clone seed <id> <name>` | Apply a named seed to a running twin |
| `checkpoint clone reset <id>` | Reset a running twin to factory state |
| `checkpoint clone tools <id>` | List MCP tools the twin exposes |
| `checkpoint clone renew <id> --ttl-seconds N` | Extend a twin's TTL metadata |
| `checkpoint clone stop <id>` | Stop a running twin session |
| `checkpoint runs list` | List recent run records |
| `checkpoint compare <run_a> <run_b>` | Criterion-level diff between two runs |
| `checkpoint traces detail [run_id]` | Inspect a persisted run record |
| `checkpoint traces export [run_id] -o out.json` | Export run record to JSON |
| `checkpoint debug usage` | Aggregate stats: total runs, avg score, LLM/tool calls |
| `checkpoint debug export <run_id> -o out.json --anonymize` | Sanitize emails/tokens before sharing |
| `checkpoint debug inspect [run_id]` | Pretty-print a run record (alias for `traces detail`) |

## Developing the dashboard

The shipped dashboard is a pre-built bundle — end users do **not** need npm.  For UI changes:

```bash
cd checkpoint/dashboard/web
npm install
npm run dev          # Vite dev server on :5173, proxies /api -> :4001

# In a second terminal:
checkpoint serve --port 4001
```

After UI changes, build and commit the bundle so the wheel ships fresh assets:

```bash
npm run build        # writes to ../static/, which is tracked in git
```

CI runs `npm run build` then `git diff --exit-code -- checkpoint/dashboard/static/` — if the committed bundle drifts from a fresh build, the PR fails.

## More

- **16 bundled scenarios** under [`scenarios/`](scenarios/) — happy paths, adversarial variants for all 7 twins, multi-clone cross-system scenarios.
- **4 example agents** under [`examples/agents/`](examples/agents/) — OpenAI / Anthropic / LangChain / MCP-client.
- **JS test suite?** See [`checkpoint-vitest/`](checkpoint-vitest/) for `@checkpoint/vitest` — all 7 twins supported.
- **MCP?** Every twin mounts a FastMCP server at `/mcp/` with tool names mirroring the official vendor MCP servers.
- **Architecture deep-dive?** See [`DESIGN.md`](DESIGN.md) — 950 lines covering every aspect of the system.
- **Self-hosting?** See [`DEPLOYMENT.md`](DEPLOYMENT.md).
- **CI?** See [`.github/workflows/checkpoint-ci.yml`](.github/workflows/checkpoint-ci.yml).

## License

MIT
