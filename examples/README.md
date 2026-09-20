# Example agents

Three agents, each a complete program you could have written yourself, and none
of them imports Checkpoint. What makes them testable is not in the code:

- [`coding-agent/`](coding-agent/) — edits a repository instead of calling a
  service: the standard library, no model, and a scenario made entirely of
  assertions over the diff it left, so it runs with no API key and no
  dependencies.
- [`tool-calling-agent/`](tool-calling-agent/) — a model with three tools, each
  a thin wrapper over a vendor SDK (PyGithub, `slack_sdk`) pointed at
  `api.github.com` and `slack.com`.
- [`mcp-agent/`](mcp-agent/) — an MCP client that declares no tools at all and
  uses whatever the server offers.

The two that call services run as they stand against the real APIs with real
credentials. Both need `OPENAI_API_KEY`: the agent's own model reads it, and so
does the judge, for the single `[P]` criterion in each of their scenarios.

There used to be four of those, one per framework. Three differed only in which
SDK spelled the same tool-calling loop, which taught the reader nothing about
Checkpoint and gave this repository three copies of one file to keep working.
The MCP agent stayed because its tool layer is genuinely a different mechanism.
If your framework is not here, the part you need to copy is the last two lines
of `agent.py` and the `checkpoint.toml` beside it.

## The whole integration

```python
if __name__ == "__main__":
    print(run(os.environ["CHECKPOINT_TASK"]))
```

Checkpoint starts the command in `checkpoint.toml`, puts the scenario's task in
`$CHECKPOINT_TASK`, and reads the final answer off stdout. An agent that takes
its task as an argument or on stdin says so with `task_via`; one that logs to
stdout writes its answer to `$CHECKPOINT_ANSWER_FILE` instead. See
[docs/getting-started.md](../docs/getting-started.md).

Calls to the services' production hostnames are routed into the twins, so the
code path under test is the one that ships — no container, no vendored client,
no test mode inside the agent. The credentials the SDKs read are replaced with
fakes that only the twins accept, so a real `GITHUB_TOKEN` cannot reach real
GitHub through an agent whose run declares the `github` twin. Everything else in
the environment is inherited as it is — the agent still needs its PATH and its
own model key — so a credential for a service this run does not replace does
reach the process, and it is the egress policy, not the environment, that keeps
it from leaving.

## Running one

Each directory has its own `checkpoint.toml` and its own scenarios, so it is a
project in its own right:

```bash
cd examples/tool-calling-agent
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...     # the agent's model, and the judge for its [P] criterion
checkpoint run
checkpoint gate
```

`coding-agent` needs neither of those two lines:

```bash
cd examples/coding-agent
checkpoint check
checkpoint run
```

To see the machinery work from anywhere, with no project and no API key, run
`checkpoint demo`.
