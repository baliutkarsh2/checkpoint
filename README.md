# Checkpoint

[![CI](https://img.shields.io/github/actions/workflow/status/baliutkarsh2/checkpoint/checkpoint-ci.yml?branch=main&label=CI)](https://github.com/baliutkarsh2/checkpoint/actions/workflows/checkpoint-ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)

**Prove your agent works before your customers find out it doesn't.**

Checkpoint runs your real agent — unmodified — against stateful copies of the
services it calls, checks what it actually did to those services, and repeats
until the pass rate means something. Then it ships or blocks the build.

```bash
pip install git+https://github.com/baliutkarsh2/checkpoint   # PyPI release pending
checkpoint demo
```

```
File an issue  github · no API key · no network
  ✓ [D]  Exactly 1 issue was created
  ✓ [D!] No issues were deleted
  ✓ [T]  The agent made at most 10 calls

  100/100  2 API calls · 3.3s
```

That took three seconds, sent nothing to the internet, and called no model. A
real agent made real HTTP calls; a real GitHub twin changed state; the criteria
were checked against that state.

## Why this exists

Three things go wrong when you test an agent the usual way.

**One run tells you almost nothing.** Agents are stochastic. The run you happen
to watch is the run you believe, and an agent that works four times in five
looks perfect until it is in front of a customer.

**Asking the model whether it succeeded is asking the defendant for a verdict.**
A judge reading the final answer scores what the agent *said*. Agents say they
filed the ticket, issued the refund, sent the message. Checkpoint scores what
changed in the service.

**Mocks test the mock.** The moment you stub the SDK, you stop testing the code
you ship — the retry logic, the pagination, the error branch. Checkpoint
intercepts TLS locally and routes `https://api.github.com` into a twin that
holds state, so the code path under test is the one that ships. No Docker, no
recorded cassettes, no changes to your agent.

## Test your agent

```bash
cd your-agent-repo
checkpoint init --command "python my_agent.py"
checkpoint run
```

`init` writes two files and touches nothing else: `checkpoint.toml` and a
starter scenario. There is no harness, no wrapper, no adapter. Checkpoint runs
the command that already runs your agent, puts the task in `$CHECKPOINT_TASK`,
and reads the final answer from stdout.

Your agent takes the task another way? `--task-via arg --task-arg --prompt`
appends it to the command line; `--task-via stdin` pipes it. It is an HTTP
service? Put `url = "http://127.0.0.1:8000/chat"` under `[agent]`. It logs to
stdout? Write the answer to `$CHECKPOINT_ANSWER_FILE` instead.

## A scenario

One markdown file. The task, and what has to be true afterwards.

```markdown
---
twins: [github]
seed: small-project
---
# File a bug

## Task
File an issue in acme/webapp titled "Login broken".

## Criteria
- [D] Exactly 1 issue was created
- [D!] No issues were deleted
- [T] The agent made at most 6 calls
- [P] The final answer quotes the issue number
```

`[D]` checks the state the agent left behind, `[T]` the calls it made, `[P]`
what it said — and only `[P]` costs a model call. `!` marks a criterion that
must pass whatever the rest score.

Each criterion becomes an assertion over the run. `checkpoint check` shows you
which one before you spend a single run on it:

```
[D]   Exactly 1 issue was created      pattern: count(created.github.issues) == 1
[D!]  No issues were deleted           pattern: count(deleted.github.issues) == 0
[T]   The agent made at most 6 calls   pattern: count(trace) <= 6
[P]   The final answer quotes ...      judged: the judge model reads the final answer
```

Write your own when you want no ambiguity and no model in the loop:

```markdown
- [D] The issue is still open  =>  count(github.issues[title == "Login broken" && state == "open"]) == 1
```

**The rule that matters:** a criterion must fail for an agent that did nothing.
"An issue exists" can already be true of the seed. "Exactly one issue was
created" cannot. `checkpoint check` is where a vacuous criterion shows itself.

## The verdict

`checkpoint run` is the loop you stay in while building. `checkpoint gate` is
what CI reads.

```
 Scenario             Pass   Rate     95% CI      pass^8   Reading
───────────────────────────────────────────────────────────────────
 file-a-bug.md       16/16   100%   [81%, 100%]     100%   stable pass
 refund-flow.md      12/16    75%   [51%, 90%]       10%   flaky

┌─── gate ───┐
│ BLOCK      │
└────────────┘
```

The gate runs every scenario N times and decides from the distribution — a
Wilson confidence interval on the pass rate, not one lucky run. `pass^8` is the
number that tends to land: an agent that passes 75% of the time completes eight
steps in a row about a tenth of the time.

| Verdict | Exit | Meaning |
|---|---|---|
| SHIP | 0 | every scenario confidently passes |
| BLOCK | 1 | a confident failure, a regression, or a scenario that failed every run |
| CONDITIONAL | 2 | enough runs to decide, results genuinely mixed |
| INCONCLUSIVE | 3 | too few runs for SHIP to be reachable; the output says how many it needs |
| ERROR | 4 | the sandbox, judge or scenarios broke — no verdict is possible, and none is invented |

Two things that are easy to get wrong and this gets right. A perfect run of
fewer than 16 scenarios cannot clear the default bar, so it reports
INCONCLUSIVE rather than a green build. And broken plumbing is never a verdict:
a missing API key, a sandbox that would not start, a criterion that could not be
evaluated — each is an ERROR, not a failing agent.

Pass rates are remembered per scenario and updated **only on a SHIP**, so a
build that used to pass and now fails reads as a regression instead of quietly
resetting the bar.

## In CI

```yaml
- uses: baliutkarsh2/checkpoint@main
  with:
    command: python my_agent.py
    runs: "16"
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Or `checkpoint gate` directly — the exit code is the whole product.
`checkpoint init --ci` writes a workflow that gates every pull request and keeps
the evidence as a build artifact.

## What you get to test against

Seven services, running locally, holding state across a multi-step run:
**GitHub, Slack, Stripe, Linear, Supabase, Discord, Google Workspace**. Each one
answers the calls the vendor's own SDK makes — PyGithub, `slack_sdk`, `stripe`,
`@linear/sdk`, `supabase-py`, `discord.py`, `google-api-python-client` — which is
checked in CI against those SDKs on every commit, so a twin bug cannot quietly
fail a correct agent. Each exposes a REST surface and an MCP server.

`checkpoint twins list` shows them and the datasets they ship with.

They also misbehave on request, which is the part you cannot rehearse against a
real API: rate limits, permission denials, read-only mode, latency, a seeded
error rate, or a targeted failure on one specific call.

```bash
checkpoint run --rate-limit 5         # the API starts refusing after 5 calls
checkpoint run --read-only            # every write is refused, and attempting one fails the run
checkpoint run --egress none          # the agent cannot reach anything but the twins
```

Testing something we do not ship? Point Checkpoint at an ASGI app of your own
and it becomes a twin like any other:

```toml
[twins.billing]
app = "mycompany.testing.billing_twin:app"
domains = ["api.billing.internal"]
```

## Agents that edit a repository

Not every agent calls an API. Point a scenario at a fixture directory and your
agent runs inside a throwaway copy of it, with the diff it leaves behind as the
thing you score:

```yaml
---
workspace: fixtures/small-repo
---
```

```
- [D] Exactly 1 file was created  =>  count(created.workspace.files) == 1
- [D!] poetry.lock was not modified
- [D] src/app.py defines main  =>  count(workspace.files[path == "src/app.py" && content ~ /def main/]) == 1
```

Your agent needs no changes: its working directory *is* the tree. The fixture
is never written to, and every run starts from a fresh copy, so sixteen gate
runs are sixteen independent attempts. A workspace is a disposable tree and a
diff, not a jail — see [the docs](docs/scenarios.md) for what that does and
does not protect you from.

## Beyond the happy path

```bash
checkpoint redteam            # adversarial scenarios, mapped to OWASP Agentic categories
checkpoint simulate refund.md # a simulated user who argues, escalates and changes their mind
checkpoint gate --certificate release.json   # a signed, verifiable record of the verdict
checkpoint report --certificate release.json # the assurance document a reviewer asks for
```

`checkpoint redteam` reports which *class* of attack lands — a prompt injection
hidden in tool output, a destructive instruction, an exfiltration attempt — and
keeps three outcomes apart that are easy to blur into one: "resisted", "nothing
landed, but the runs cannot prove it", and "the runs could not be scored at
all". Only the first is a pass, and none of them is invented from an absence.

The bundled pack ships inside the package and covers all ten OWASP Agentic
categories, one scenario each, across all seven twins. Every one of them pairs
its attack with a legitimate task the agent is expected to finish, so an agent
that answers "I won't do that" and stops scores no better than one that fell
for it.

## How it works

```
checkpoint.toml → Agent          the command that already runs your agent
                  Sandbox        twins + a TLS intercept proxy + an egress policy
                  criteria       compiled to assertions over what changed
                  judge          only for [P], only when one is needed
                  verdict        a pass rate with a confidence interval
```

The intercept proxy is Checkpoint's own: it mints a local CA, serves per-host
certificates, and hands your agent the environment every major HTTP client
respects. Your agent's own calls to OpenAI or Anthropic pass through untouched;
everything else is subject to the egress policy and reported when blocked.

Everything the CLI does is importable:

```python
from checkpoint import Agent, parse_file, run_scenario

result = run_scenario(parse_file("scenarios/refund.md"), Agent(command="python my_agent.py"))
print(result.score, [c.text for c in result.criteria if not c.passed])
```

There is a pytest plugin too, so a scenario can be an ordinary test:

```python
def test_refund_flow(checkpoint_run):
    result = checkpoint_run("scenarios/refund.md")
    assert result.score == 100, [c.text for c in result.criteria if not c.passed]
```

## Install

```bash
pip install git+https://github.com/baliutkarsh2/checkpoint
export OPENAI_API_KEY=sk-...     # only for [P] criteria; assertion-only scenarios need nothing
```

Python 3.11 or newer. Nothing to build, nothing to run alongside it. Check the
machine with `checkpoint doctor`, which starts a twin and self-tests the proxy
rather than taking your word for it.

**Not on PyPI yet.** The distribution will be `checkpoint-agents`; the bare name
`checkpoint` on PyPI is an unrelated project.

**Any judge model.** Pass `--model` a `gpt-*`, `claude-*` or `gemini-*` name, or
set it once under `[judge]` in `checkpoint.toml`. For a local or self-hosted
model, point `CHECKPOINT_LLM_BASE_URL` at any OpenAI-compatible endpoint. Claude
needs `pip install checkpoint-agents[anthropic]`; the rest need nothing extra.

## Where it is honest about itself

- The twins reproduce the endpoints scenarios exercise, not every corner of
  every API. `checkpoint twins list` is the inventory, and the conformance
  suites in `tests/sdk/` are the evidence.
- A `[P]` criterion is a model's opinion. It is scored separately, its reasoning
  is recorded, and it can answer "unknown" rather than guess.
- `checkpoint redteam generate` writes attack *candidates*. A model that writes
  the test is not also the authority on whether you passed it.
- A run that could not be scored is never counted as a pass or a failure.

## Commands

```
checkpoint init      point Checkpoint at your agent
checkpoint demo      see it work — offline, no API key
checkpoint run       run scenarios against your agent
checkpoint gate      decide whether this build ships
checkpoint redteam   run adversarial scenarios
checkpoint simulate  hold a conversation as a simulated user
checkpoint new       write a new scenario
checkpoint check     check scenarios before you run them
checkpoint twins     the services scenarios run against
checkpoint cert      issue and verify signed verdicts
checkpoint report    build an assurance report
checkpoint runs      past runs: list, show, compare, export
checkpoint view      open the dashboard
checkpoint mcp       serve Checkpoint over MCP
checkpoint doctor    check this machine
```

`checkpoint mcp` puts all of this inside your coding agent: any MCP client can
list scenarios, run one, and gate the build while it writes the very agent under
test.

## Documentation

[Getting started](docs/getting-started.md) ·
[Scenarios](docs/scenarios.md) ·
[Twins](docs/twins.md) ·
[The gate](docs/gate.md) ·
[Architecture](docs/architecture.md) ·
[Self-hosting](docs/self-hosting.md)

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Read the evaluator before you trust the verdict — the assertion language is
documented and tested in `checkpoint/eval/expr.py`, and every criterion's
assertion is stored with the run.

Apache-2.0. See [LICENSE](LICENSE) and [SECURITY.md](SECURITY.md).
