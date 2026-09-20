# MCP-client agent

The other common shape: the agent declares no tools of its own. It connects to
an MCP server, asks what that server can do, and hands the answer to the model.
`agent.py` is the whole client — a connect, a `list_tools`, and a loop.

Every twin serves its operations over MCP as well as over its REST API, at
`/mcp/` under its base URL. `checkpoint twins tools github` lists them.

## Install

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

## Run it

From this directory:

```bash
checkpoint run
checkpoint gate
```

## The server address

An MCP client is configured with its server's URL rather than compiling one in,
which is the whole reason this example needs no code change to be tested:

```python
MCP_URL = os.environ.get("GITHUB_MCP_URL") or (
    os.environ.get("CHECKPOINT_GITHUB_URL", "https://api.githubcopilot.com").rstrip("/") + "/mcp/"
)
```

The sandbox exports `CHECKPOINT_GITHUB_URL` for the GitHub twin it started, and
`CHECKPOINT_<TWIN>_URL` for every other twin in the run.

## What the scenario checks

`scenarios/close-a-fixed-issue.md` seeds `acme/webapp` with two open issues and
asks for one of them to be commented on and closed. The criteria are pinned
assertions over what changed:

```
- [D] The Safari issue is closed  =>  github.issues[key == "acme/webapp#2"].state == "closed"
- [D] Only that one issue changed  =>  count(changed.github.issues) == 1
```

The second one is there because an agent with 34 tools and a vague instruction
is quite capable of tidying the whole repository on its way past. A scenario
that only checks the thing you asked for will not notice.
