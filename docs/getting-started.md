# Getting started

## Install

```bash
pip install git+https://github.com/baliutkarsh2/checkpoint   # PyPI release pending
```

Python 3.11 or newer. Nothing else: no Docker, no daemon, no account. A judge
model key is needed only for `[P]` criteria, and only when you write one.

`checkpoint doctor` reports whether this machine can run a scenario, and says
what is missing when it cannot.

## See it work

```bash
checkpoint demo
```

A small agent files an issue against a real GitHub twin, and the criteria are
checked against the state the twin ended up in:

```
File an issue  github · no API key · no network
  ✓ [D]  Exactly 1 issue was created
  ✓ [D!] No issues were deleted
  ✓ [T]  The agent made at most 10 calls

  100/100  2 API calls · 2.6s
```

Nothing there was mocked. The agent made HTTP calls, a stateful service
answered them, and the score came from that service's final state rather than
from what the agent claimed.

## Point it at your agent

```bash
checkpoint init --command "python my_agent.py"
```

Three files, none of them code:

```
  + checkpoint.toml
  + scenarios/quickstart.md
  + .gitignore
```

Nothing that already exists is touched, and nothing is written into your
agent's source. A repository that already has `.github/` also gets a workflow
that gates every pull request, and one that already has `.claude/` gets a skill
so your coding agent can drive Checkpoint; `--ci`/`--no-ci` and
`--skill`/`--no-skill` decide either explicitly.

### How your agent receives the task

By default Checkpoint puts the scenario's task in `$CHECKPOINT_TASK` and reads
the final answer from whatever the process printed to stdout — plain text, or
JSON with a `text`, `answer`, `output` or `content` key. For most agents that
is two lines at the bottom of a file you already have:

```python
if __name__ == "__main__":
    print(run(os.environ["CHECKPOINT_TASK"]))
```

Agents that take their task some other way say so in `checkpoint.toml`:

```toml
[agent]
command = "node agent.js"
task_via = "arg"        # env (default), arg, or stdin
task_arg = "--prompt"   # with task_via = "arg": the flag the task follows
```

An agent that logs to stdout has nowhere clean to put its answer, so it writes
the answer to the file named by `$CHECKPOINT_ANSWER_FILE` instead; that always
wins over stdout. An agent that is already an HTTP service is configured with
`url` in place of `command`, and receives `POST {url}` with a JSON body of
`{"task", "messages", "session_id"}`.

One more file is optional: an agent that wants its own reasoning shown next to
the result appends JSON lines to `$CHECKPOINT_AGENT_TRACE_FILE`, one object per
event, in whatever shape it already produces. The dashboard renders the
messages and tool calls it recognizes. Writing nothing there costs nothing —
the calls the agent made to the twins are recorded either way, by the twins.

### What your agent finds in its environment

Before starting the command, Checkpoint overwrites every credential belonging
to a service in the run with a fake one — `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`,
`STRIPE_API_KEY` and so on. A real token in your shell therefore never reaches
the agent under test, and cannot reach a real API through it. The twins'
direct URLs are in `CHECKPOINT_<TWIN>_URL`, though most agents never need them:
calls to `https://api.github.com` are routed into the GitHub twin already.

## First run

```bash
checkpoint run
```

With no arguments this runs every scenario in `scenarios/`. Name a file to run
one, `-n` to run it several times, `--tag` to select a subset:

```bash
checkpoint run scenarios/quickstart.md
checkpoint run -n 5 --tag github         # only scenarios whose `tags:` list it
checkpoint run -v                        # stream the agent's own output
```

Before the first run, see how each criterion will be decided:

```bash
checkpoint check
```

```
Starter scenario: file an issue  quickstart.md · github
        Criterion                                   Decided by
──────────────────────────────────────────────────────────────────────────────────────
[D]     Exactly 1 issue was created                 pattern: count(created.github.issues) == 1
[D]     An issue titled "Add login button" exists   pattern: exists(github.issues[title == "Add
                                                    login button"])
[D!]    No issues were deleted                      pattern: count(deleted.github.issues) == 0
[T]     The agent made at most 10 calls             pattern: count(trace) <= 10
[P]     The final answer quotes the number of the   judged: the judge model reads the final
        issue it created                            answer
  ready, 1 judged by a model.
```

A criterion with no assertion behind it is compiled by the judge model at run
time instead, and `check` warns about each one — see
[Scenarios](scenarios.md#pinned-assertions) for how to write your own.

`checkpoint run` exits 1 if any run failed a criterion and 2 if the sandbox
could not be set up. It is a development command; it is not the thing to put in
CI.

## First gate

```bash
checkpoint gate
```

Sixteen runs of every scenario, and one verdict from the distribution:

```
 Scenario            Pass   Rate     95% CI      pass^8   Reading
──────────────────────────────────────────────────────────────────
 file-an-issue.md   16/16   100%   [81%, 100%]     100%   stable pass
┌──── gate ────┐
│ SHIP  exit 0 │
└──────────────┘
```

Only SHIP exits 0. A single clean run cannot produce a SHIP, by design — see
[The gate](gate.md) for the verdicts, the exit codes, and why sixteen.

## After a run

```bash
checkpoint runs list          # what has run lately
checkpoint runs show          # the last run: every criterion and why it held
checkpoint runs trace         # every API call the agent made, in order
checkpoint runs compare A B   # what changed between two runs
checkpoint view --open        # all of it in a browser
```
