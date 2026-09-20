# Example agents

Two agents, each a complete program you could have written yourself and each
runnable on its own with real credentials against the real services. Neither
imports Checkpoint. What makes them testable is not in the code:

- [`tool-calling-agent/`](tool-calling-agent/) — a model with three tools, each
  a thin wrapper over a vendor SDK (PyGithub, `slack_sdk`) pointed at
  `api.github.com` and `slack.com`.
- [`mcp-agent/`](mcp-agent/) — an MCP client that declares no tools at all and
  uses whatever the server offers.

There used to be four, one per framework. Three of them differed only in which
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
fakes that only the twins accept, so a real token in your shell cannot reach a
real API through the agent under test.

## Running one

Each directory has its own `checkpoint.toml` and its own scenarios, so it is a
project in its own right:

```bash
cd examples/tool-calling-agent
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
checkpoint run
checkpoint gate
```

To see the machinery work with no API key and no dependencies at all, run
`checkpoint demo` from anywhere.
