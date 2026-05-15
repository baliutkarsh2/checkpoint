# checkpoint

**Test your AI agent against stateful synthetic GitHub / Slack / Stripe / Linear / Supabase / Discord / Google Workspace. Score 0-100 with deterministic + LLM checks. No real-API credits burned.**

## Supported twins

GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace. Each twin is a
full FastAPI app with REST + MCP tool surface, named seeds, and introspection
endpoints (`/_health`, `/_state`, `/_trace`, `/_reset`, `/_seed/<name>`).

## Install

```bash
pip install -e .
pip install mitmproxy docker fastapi uvicorn
export OPENAI_API_KEY=sk-...
```

## Quickstart

```bash
checkpoint init          # scaffold harness.py + .checkpoint.json + starter scenario
checkpoint run scenario.md           # run one scenario, print score
checkpoint run scenarios/ --tag smoke  # run filtered directory
checkpoint validate scenario.md      # lint scenario before running
checkpoint replay                    # replay last run's API trace
checkpoint doctor                    # check environment readiness
```

## Dashboard

```bash
pip install checkpoint
checkpoint serve         # http://127.0.0.1:4001
```

A local web UI for browsing run history, comparing runs, watching live clones,
and launching new runs from the browser. Single-page React app served by the
same FastAPI process that exposes the JSON API.

What you get:

- **Runs browser** — score sparklines, criterion-level pass/fail, deep trace
  inspector with per-event request/response bodies and "copy as curl"
- **Run launcher** — pick a scenario, hit start, watch stdout/stderr stream
  in real time over Server-Sent Events
- **Live clone panel** — auto-updates as `checkpoint clone start` adds/removes twins
- **Compare flow** — tick two runs in the table, click Compare for a side-by-side diff
- **Trend report** — per-scenario sparkline + per-criterion pass-rate table + flaky detection
- **OpenAPI Swagger UI** at `/api/docs` — every endpoint typed and try-able
- **Prometheus metrics** at `/metrics` — request counts, durations, jobs, SSE subscribers
- **Command palette** (`⌘K` / `Ctrl-K`), keyboard nav (`g r`, `g s`, `g p`), dark mode (`d`)

### Security

`checkpoint serve` defaults to `127.0.0.1` and rate-limits writes (30 / 10s / IP).
**`--host 0.0.0.0` exposes the run-launcher API to your LAN — only do this on
trusted networks.** The job-launch endpoint accepts only the typed `scenario`,
`docker`, and `harness` fields; arbitrary CLI flags are rejected by the schema.

### Developing the dashboard

The shipped dashboard is a pre-built bundle — end users do **not** need npm.
For UI changes:

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

CI runs `npm run build` then `git diff --exit-code -- checkpoint/dashboard/static/` —
if the committed bundle drifts from a fresh build, the PR fails.

## Mental model

A **twin** is a stateful synthetic SaaS API running locally. A **scenario** is a
markdown file with `## Setup` (starting state), `## Prompt` (agent task), and
`## Success Criteria` (`[D]` deterministic + `[P]` LLM-judged). A **harness** is
your agent script that reads `CHECKPOINT_<CLONE>_URL` env vars and prints
`{"text": "final answer"}` to stdout. Checkpoint spins up the twins, runs the
harness, grades criteria, returns a score.

```
checkpoint run scenario.md --harness "python harness.py"
    |-- spins up twin(s) as local FastAPI/uvicorn servers
    |-- each twin: REST surface + MCP at /mcp/ + introspection
    |-- runs harness subprocess with CHECKPOINT_<CLONE>_URL env vars
    |-- collects trace (every API call) + state (post-run snapshot)
    `-- 3-stage eval: regex patterns -> LLM-JSON schema -> GPT judge
```

For Docker-isolated runs with TLS interception:

```bash
checkpoint run scenario.md --docker
```

## CLI reference

| Command | Purpose |
|---|---|
| `checkpoint init` | Scaffold integration in the current repo |
| `checkpoint serve` | Start the web dashboard at http://127.0.0.1:4001 |
| `checkpoint run <scenario.md>` | Run a scenario, print score |
| `checkpoint run <dir/> --tag smoke` | Run all scenarios filtered by tag |
| `checkpoint validate <scenario.md>` | Parse and lint a scenario file |
| `checkpoint replay [run_id]` | Replay API trace from a past run |
| `checkpoint doctor` | Verify environment (Docker, ports, API key) |
| `checkpoint scenario list` | Enumerate scenarios in cwd |
| `checkpoint clone start <id>` | Spin up a long-lived twin session |
| `checkpoint clone inspect <id>` | Show clone metadata and request counts |
| `checkpoint clone stop <id>` | Stop a running twin session |
| `checkpoint runs list` | List recent run records |
| `checkpoint compare <run_a> <run_b>` | Criterion-level diff between two runs |
| `checkpoint traces detail [run_id]` | Inspect a persisted run record |
| `checkpoint traces export [run_id] -o out.json` | Export run record to JSON |

## More

- **15 bundled scenarios** under [`scenarios/`](scenarios/) -- GitHub happy path,
  adversarial variants for all 7 twins, multi-clone cross-system scenarios.
- **JS test suite?** See [`checkpoint-vitest/`](checkpoint-vitest/) for
  `@checkpoint/vitest` -- all 7 twins supported.
- **MCP?** Every twin mounts a FastMCP server at `/mcp/` with tool names mirroring
  the official vendor MCP servers.
- **Docker sandbox?** See [`checkpoint/sandbox/`](checkpoint/sandbox/) for the
  pre-baked image with the TLS sidecar.
- **CI?** See [`.github/workflows/checkpoint-ci.yml`](.github/workflows/checkpoint-ci.yml).

## License

MIT
