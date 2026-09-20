# Tool-calling agent

The common production shape: a model with a handful of tools, each one a thin
wrapper over a vendor SDK. `agent.py` uses PyGithub against `api.github.com`
and `slack_sdk` against `slack.com`, and a chat-completions loop to decide what
to call. Nothing in it knows Checkpoint exists except the last two lines, which
read the task out of the environment and print the answer.

## Install

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

The GitHub and Slack credentials are not yours to supply here: the sandbox
exports fake `GITHUB_TOKEN` and `SLACK_BOT_TOKEN` values, overwriting whatever
your shell had. A real token in your environment cannot reach a real API
through this agent, because the agent never sees it.

## Run it

From this directory:

```bash
checkpoint run
checkpoint gate
```

`run` executes `scenarios/incident-triage.md` once and prints what held and what
did not. `gate` runs it sixteen times and turns the pass rate into a SHIP or
BLOCK — the number CI reads.

## What the scenario checks

The GitHub twin is seeded with two open issues and the Slack twin with a busy
`#engineering`, so "an issue exists" and "a message exists" are both true before
the agent starts. Every criterion is written against `created.…` instead, and
the assertions are pinned in the file, so the same run scores the same way
whoever runs it:

```
- [D] Exactly one issue was filed  =>  count(created.github.issues) == 1
```

One criterion is left to the judge — whether the final answer actually names
what it filed and where it posted — because no assertion decides that honestly.

## Pointing it at your own stack

Swap the tool bodies for your own SDK calls and leave the rest. If a service you
call is not one of the seven bundled twins, write the twin and declare it under
`[twins.<name>]` in `checkpoint.toml`; see `docs/twins.md`.
