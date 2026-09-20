# checkpoint.toml

One file, at the root of your project, holding everything Checkpoint needs to
run your agent. `checkpoint init` writes it; this page is what it can contain.

Checkpoint finds it by walking up from the working directory, so commands work
from anywhere inside the project, and every path in it resolves against the file
rather than against your shell's current directory.

**Unknown keys are errors.** A setting that looks applied and is not is the
failure this file exists to prevent, so a typo stops the command and names what
is supported instead of being ignored.

**Precedence is the same everywhere: flag > environment > file > default.** A
flag you just typed always wins; the file is where you stop typing it.

```toml
[agent]
command = "python my_agent.py"

[judge]
model = "gpt-5.6-luna"

[gate]
runs = 16
```

## `[agent]` — how to start the thing under test

| Key | Default | Means |
|---|---|---|
| `command` | — | The command that already runs your agent. No wrapper, no harness. |
| `url` | — | An HTTP endpoint that answers tasks instead of a command. Set both to start the command and then talk to it. |
| `task_via` | `env` | How the task reaches the agent: `env`, `arg` or `stdin`. |
| `task_env` | `CHECKPOINT_TASK` | With `task_via = "env"`, the variable holding the task. |
| `task_arg` | — | With `task_via = "arg"`, the flag the task follows, e.g. `--prompt`. |
| `cwd` | the project root | Working directory for the agent, resolved against this file. |
| `env` | — | Extra environment variables, as a table: `env = { API_MODE = "test" }`. |
| `timeout` | — | Seconds before a run is abandoned and its process tree killed. |
| `name` | — | What to call this agent in reports and certificates. |

A script named in `command` is resolved against this file, so `python agent.py`
finds your agent even when the run starts somewhere else — which is what happens
for a scenario with a `workspace:`.

## `[judge]` — the model that scores `[P]` criteria

| Key | Default | Means |
|---|---|---|
| `model` | `gpt-5.6-luna` | Any `gpt-*`, `claude-*` or `gemini-*` name. Set `CHECKPOINT_LLM_BASE_URL` to use any OpenAI-compatible endpoint instead. |
| `samples` | `1` | How many times to ask about each criterion. More than one costs more and disagrees less; a judge that flips between samples reports `unknown` rather than guessing. |

Only `[P]` criteria reach a model on every run. `[D]` and `[T]` are assertions:
one the compiler recognises by pattern, or one you pinned with `=>`, never calls
a model at all. A `[D]`/`[T]` phrased so that no pattern matches is translated
into an assertion by the judge model **once** and then cached, so it costs one
call the first time and nothing afterwards.

`checkpoint check` prints which of the three each criterion is, so you can see
what a suite will cost before running it. A scenario whose criteria all show as
`pattern:` or `pinned:` needs no key at all — which is why `checkpoint demo`
works offline.

## `[sandbox]` — the world the agent runs in

| Key | Default | Means |
|---|---|---|
| `intercept` | `true` | Route calls to production hostnames (`https://api.github.com`) into the twins. |
| `egress` | `llm` | What the agent may reach besides the twins: `open`, `llm` (LLM providers plus `allow_hosts`), or `none`. |
| `allow_hosts` | — | Extra hostnames the agent may reach, as a list. |

## `[gate]` — what it takes to ship

| Key | Default | Means |
|---|---|---|
| `runs` | `16` | Runs per scenario. Fewer than 16 cannot reach SHIP at the default thresholds, and the gate says so rather than passing the build. |
| `pass_threshold` | `80` | Score out of 100 a single run needs to count as a pass. |
| `ship_min` | `0.80` | The pass rate's lower confidence bound must clear this for SHIP. |
| `block_max` | `0.50` | An upper bound below this is a confident failure: BLOCK. |
| `confidence` | `0.95` | Confidence level for the Wilson interval. |
| `regression_drop` | `0.20` | A fall of this much against the stored baseline reads as a regression. |
| `allow_conditional` | `false` | Exit 0 on CONDITIONAL. Off by default: only SHIP is a green build. |
| `strict` | `false` | Refuse CONDITIONAL even when `allow_conditional` is set. |
| `concurrency` | `4` | Runs of one scenario to execute at once, each in its own sandbox. Scenarios still run one after another; this splits the N runs of each. Capped at the CPU count, because a worker holds a whole sandbox open and the ceiling is memory rather than cores. |

## `[scenarios]` — where the tests are

| Key | Default | Means |
|---|---|---|
| `path` | `scenarios` | A single directory or file. |
| `paths` | — | Several, as a list: `paths = ["suites/smoke", "suites/regression"]`. |

## `[twins.<name>]` — a twin of your own

Point Checkpoint at any ASGI app in your repository and it becomes a twin like
the built-in seven: scenarios name it, the sandbox starts it, and the proxy
routes its production hostnames into it.

```toml
[twins.billing]
app = "mycompany.testing.billing_twin:app"
domains = ["api.billing.internal"]
token_env = ["BILLING_API_KEY"]
```

| Key | Default | Means |
|---|---|---|
| `app` | — | Import path to an ASGI app, `module:attribute`. Required. |
| `domains` | — | Production hostnames to route into it. |
| `title` | the name | What to call it in the dashboard and reports. |
| `token` | generated | The fake credential it accepts. One is invented if you do not set it, because an SDK reading an empty variable fails for a reason that has nothing to do with your agent. |
| `token_env` | — | Variables the agent's SDK reads that credential from. |
| `auth_scheme` | — | How the credential is presented, e.g. `bearer`. |
| `production_url` | — | The real base URL, for reports that say what was replaced. |
| `extra_env` | — | Extra variables to set for the agent when this twin is in the sandbox. |
| `docs` | — | A link to the real API's documentation, shown beside the twin. |

A project twin cannot take a built-in's name: `[twins.github]` is an error
rather than a silent override, because a scenario that says `twins: [github]`
must mean the same thing in every repository.

## Environment variables

Settings that belong to the machine rather than the project, so they are not in
this file:

| Variable | Means |
|---|---|
| `CHECKPOINT_JUDGE_MODEL` | Overrides `[judge] model`. |
| `CHECKPOINT_LLM_BASE_URL` | Sends every model call to an OpenAI-compatible endpoint. |
| `CHECKPOINT_HOME` | One directory for the signing key *and* the project's state, which otherwise default to different places: the key lives in `~/.checkpoint/keys`, while baselines and the run database are `.checkpoint/` in the working directory (the project root, for `checkpoint view`). |
| `CHECKPOINT_PORT` | Port for `checkpoint view`. |
| `CHECKPOINT_DASHBOARD_API_KEY` | Required before the dashboard will bind off loopback. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Provider keys, read by the judge only when it needs one. |

`checkpoint doctor` checks the machine and the project — Python version, TLS
interception, twins, `checkpoint.toml`, the agent command, scenarios and the
judge model — not this table. The only variables it reflects are the judge's:
the model it resolved, which `CHECKPOINT_JUDGE_MODEL` overrides; whether that
provider's key is set; and `CHECKPOINT_LLM_BASE_URL` when that is supplying the
endpoint. `CHECKPOINT_HOME`, `CHECKPOINT_PORT` and `CHECKPOINT_DASHBOARD_API_KEY`
are read where they are used and reported nowhere.
