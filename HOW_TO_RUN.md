# How to Run and Test Everything

## Overview

Checkpoint is an agent evaluation platform that provides 7 stateful SaaS clones (twins) for safe agent testing. This guide covers installation, running twins, evaluating agents, and running the test suite.

---

## 1. Installation

```bash
# Clone the repo
git clone https://github.com/Aaditya2605/checkpoint.git
cd checkpoint

# Install with all dependencies (includes mcp, fastapi, uvicorn, httpx, etc.)
pip install -e ".[dev]"
```

---

## 2. Available SaaS Twins

| Twin | Module | Domain | Bootstrap Token |
|------|--------|--------|-----------------|
| GitHub | `checkpoint.twins.github` | `api.github.com` | `ghp_AaBbCc...` |
| Slack | `checkpoint.twins.slack` | `slack.com` | `xoxb-123456789012...` |
| Stripe | `checkpoint.twins.stripe` | `api.stripe.com` | `sk_live_51Abc123...` |
| Linear | `checkpoint.twins.linear` | `api.linear.app` | `lin_api_AaBbCc...` |
| Supabase | `checkpoint.twins.supabase` | `supabase.co` | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...` |
| Discord | `checkpoint.twins.discord` | `discord.com` | `Bot checkpoint.discord.twin...` |
| Google Workspace | `checkpoint.twins.google_workspace` | `gmail.googleapis.com`, `www.googleapis.com` | `ya29.checkpoint_google_workspace...` |

---

## 3. Starting a Twin

### Quick start (single twin)

```bash
python -m uvicorn checkpoint.twins.github:app --port 8001
python -m uvicorn checkpoint.twins.slack:app --port 8002
python -m uvicorn checkpoint.twins.linear:app --port 8003
python -m uvicorn checkpoint.twins.supabase:app --port 8004
python -m uvicorn checkpoint.twins.discord:app --port 8005
python -m uvicorn checkpoint.twins.google_workspace:app --port 8006
```

### Verify health

```bash
curl http://localhost:8001/_health
# {"status":"ok"}
```

### Load a seed

```bash
# Seed the GitHub twin with a project that has open issues
curl -X POST http://localhost:8001/_seed/two-open-issues

# Available seeds per twin:
# GitHub: empty, two-open-issues, merged-pr, label-triage
# Slack: empty, engineering-team
# Stripe: empty, subscription-billing, refund-flow
# Linear: empty, small-project, sprint-planning, backlog-triage
# Supabase: empty, small-app, ecommerce
# Discord: empty, small-server, incident-response
# Google Workspace: empty, small-team
```

### Inspect state

```bash
# View current twin state
curl http://localhost:8001/_state | python -m json.tool

# View request trace (all API calls the agent made)
curl http://localhost:8001/_trace | python -m json.tool

# Reset to fresh state
curl -X POST http://localhost:8001/_reset
```

---

## 4. MCP Servers

Each twin exposes an MCP server at `/mcp/` (streamable HTTP transport).

```bash
# Start the twin
python -m uvicorn checkpoint.twins.discord:app --port 8005

# The MCP server is at http://localhost:8005/mcp/
# Connect any MCP client (e.g. Claude Desktop) to that URL
# Authentication: use the same bootstrap token as the REST API
```

### Available tools per twin

| Twin | Tools |
|------|-------|
| GitHub | 12 tools — issues, PRs, repos, branches, labels, comments, workflows |
| Slack | 8 tools — messages, channels, threads, reactions, users |
| Stripe | 14 tools — customers, products, prices, payment intents, refunds, invoices, subscriptions |
| Linear | 25 tools — issues, teams, projects, cycles, labels, workflow states, comments |
| Supabase | 25 tools — PostgREST query/insert/update/delete, auth users, storage buckets/objects |
| Discord | 31 tools — guilds, channels, messages, reactions, roles, webhooks, pins |
| Google Workspace | 35 tools — Gmail (threads, messages, labels, drafts), Drive (files, folders, permissions) |

---

## 5. Clone Manager (Long-lived sessions)

Start a persistent twin without keeping a terminal open:

```bash
# Start a clone
checkpoint clone start github
# → prints URL, port, and MCP URL

# Check status
checkpoint clone inspect github

# Stop
checkpoint clone stop github
```

Clone registry stored at `.checkpoint/cache/clones.json`.

---

## 6. Running Scenarios

### Single scenario

```bash
checkpoint run scenarios/github-happy-path.md \
  --harness "python my_agent.py" \
  --clone github
```

### Directory of scenarios

```bash
checkpoint run scenarios/ \
  --harness "python my_agent.py"
```

### Filter by tag

```bash
checkpoint run scenarios/ \
  --harness "python my_agent.py" \
  --tag smoke
```

### With specific seed

```bash
checkpoint run scenarios/discord-incident-response.md \
  --harness "python my_agent.py"
# The scenario's Config: section specifies seed: discord=incident-response
```

### Save traces

```bash
checkpoint run scenarios/github-happy-path.md \
  --harness "python my_agent.py" \
  --trace-out run-output.json
```

### Compare two runs

```bash
# List recent runs
checkpoint runs list

# Compare baseline vs candidate
checkpoint compare <run_id_a> <run_id_b>

# JSON output
checkpoint compare <run_id_a> <run_id_b> --json
```

---

## 7. Harness Integration

Your agent harness receives twin URLs via environment variables:

```bash
CHECKPOINT_BASE_URL=http://127.0.0.1:<port>
CHECKPOINT_GITHUB_URL=http://127.0.0.1:<port>
CHECKPOINT_SLACK_URL=http://127.0.0.1:<port>
CHECKPOINT_STRIPE_URL=http://127.0.0.1:<port>
CHECKPOINT_LINEAR_URL=http://127.0.0.1:<port>
CHECKPOINT_SUPABASE_URL=http://127.0.0.1:<port>
CHECKPOINT_DISCORD_URL=http://127.0.0.1:<port>
CHECKPOINT_GOOGLE_WORKSPACE_URL=http://127.0.0.1:<port>
```

Bootstrap tokens are also available:
```bash
CHECKPOINT_GITHUB_TOKEN=ghp_AaBbCc...
CHECKPOINT_LINEAR_TOKEN=lin_api_AaBbCc...
CHECKPOINT_DISCORD_TOKEN=Bot checkpoint.discord.twin...
# etc.
```

### Minimal harness example

```python
import os, httpx, sys, json

# Read the target URLs from env
github_url = os.environ["CHECKPOINT_GITHUB_URL"]
token = os.environ["CHECKPOINT_GITHUB_TOKEN"]

# Your agent does work here using the twin URLs
r = httpx.post(f"{github_url}/repos/acme/api/issues",
               headers={"Authorization": f"Bearer {token}"},
               json={"title": "Fix the bug"})

# Output must be JSON to stdout
sys.stdout.write(json.dumps({"text": "Created issue " + r.json()["number"]}))
```

---

## 8. Running the Test Suite

### All tests

```bash
python -m pytest tests/ -q
```

### Twin REST API tests

```bash
# GitHub + Slack + Stripe (original)
python -m pytest tests/twins/test_slack.py tests/twins/test_stripe.py -q

# Linear
python -m pytest tests/twins/test_linear.py -q

# Supabase
python -m pytest tests/twins/test_supabase.py -q

# Discord
python -m pytest tests/twins/test_discord.py -q

# Google Workspace
python -m pytest tests/twins/test_google_workspace.py -q
```

### MCP server tests (require uvicorn, boots subprocesses)

```bash
python -m pytest tests/test_mcp_github.py tests/test_mcp_slack.py tests/test_mcp_stripe.py -q
python -m pytest tests/test_mcp_linear.py tests/test_mcp_supabase.py -q
python -m pytest tests/test_mcp_discord.py tests/test_mcp_google_workspace.py -q
```

### Checker and evaluator tests

```bash
python -m pytest tests/test_checker_regex_catalog.py tests/test_checker_llm.py -q
```

### CLI tests

```bash
python -m pytest tests/test_cli_tag_and_autoload.py tests/test_cli_compare_runs.py -q
```

### Clone manager and runner tests

```bash
python -m pytest tests/test_phase7_clone_manager.py tests/test_multi_clone_runner.py -q
```

---

## 9. Scenario Format

```markdown
# Scenario Title

## Prompt

Natural language task description for the agent...

## Success Criteria

- [D] exactly 2 issues are open          ← Deterministic (regex-checked)
- [D] at least 1 discord message exists  ← Deterministic
- [P] The agent posted a helpful message ← Probabilistic (LLM-judged)

## Config

clones: github, slack        # which twins to start
seed: github=two-open-issues # optional seed per clone
timeout: 120                 # seconds
tags: smoke, regression      # for --tag filter
runs: 3                      # number of runs (default 1)
```

### Criterion syntax (Deterministic)

| Pattern | Example |
|---------|---------|
| `exactly N <noun>` | `exactly 2 issues are open` |
| `at least N <noun>` | `at least 1 discord message exists` |
| `at most N <noun>` | `at most 3 draft emails exist` |
| `no new <noun>` | `no new payment intents created` |
| `a <noun> titled "X" exists` | `a drive file titled "Q1 Roadmap" exists` |
| `<noun> count equals N` | `gmail label count equals 5` |

### Supported resource nouns

GitHub: `issue`, `pull request`, `pr`, `branch`, `repo`, `label`, `comment`, `workflow run`  
Slack: `channel`, `message`, `reaction`  
Stripe: `customer`, `product`, `price`, `payment intent`, `refund`, `invoice`, `subscription`  
Linear: `linear issue`, `linear project`, `cycle`, `sprint`, `team`  
Supabase: `bucket`, `object`, `auth user`  
Discord: `guild`, `discord channel`, `discord message`, `role`, `webhook`  
Google Workspace: `email`, `gmail message`, `thread`, `draft`, `gmail label`, `drive file`

---

## 10. Available Scenarios

| Scenario | Clones | Tags |
|----------|--------|------|
| `github-happy-path.md` | github | — |
| `github-adversarial.md` | github | — |
| `slack-incident-response.md` | slack | — |
| `stripe-refund-controls.md` | stripe | — |
| `linear-issue-triage.md` | linear | linear |
| `supabase-data-ops.md` | supabase | supabase |
| `discord-incident-response.md` | discord | discord, incident-response, ops |
| `google-workspace-email-ops.md` | google-workspace | google-workspace, gmail, drive |
| `multi-clone-cross-system.md` | github, slack | — |
| `archal-verbatim-github.md` | github | — |

---

## 11. Proxy / TLS Interception (Advanced)

For scenarios where your agent uses real API endpoints that need to be redirected:

```bash
# Run with Docker (TLS sidecar)
checkpoint run scenarios/github-happy-path.md \
  --harness "python my_agent.py" \
  --docker \
  --harness-dir ./docker-harness/
```

The proxy intercepts calls to `api.github.com`, `slack.com`, `api.stripe.com`, `api.linear.app`, `supabase.co`, `discord.com`, `gmail.googleapis.com`, `www.googleapis.com` and routes them to the twins.

---

## 12. CI Integration

The repo includes `.github/workflows/checkpoint-ci.yml`. To use it:

```yaml
# In your agent repo's CI:
- name: Run checkpoint scenarios
  run: |
    pip install checkpoint
    checkpoint run scenarios/ --harness "python run_agent.py"
```

Set the following secrets in your GitHub repo settings:
- `OPENAI_API_KEY` — for the LLM judge
- Any API keys your harness needs

---

## Quick Reference

```bash
# Install
pip install -e ".[dev]"

# Start a twin
python -m uvicorn checkpoint.twins.github:app --port 8001

# Load seed
curl -X POST http://localhost:8001/_seed/two-open-issues

# Run scenario
checkpoint run scenarios/github-happy-path.md --harness "python agent.py"

# List runs
checkpoint runs list

# Compare runs
checkpoint compare <run_a> <run_b>

# Run tests
python -m pytest tests/ -q
```
