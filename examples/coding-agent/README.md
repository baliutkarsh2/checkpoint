# An agent that edits files

The other examples test agents that call services. This one tests an agent that
changes a repository — a coding agent, a migration tool, a docs generator. It
has no model and no dependencies, so it runs anywhere:

```bash
cd examples/coding-agent
checkpoint check
checkpoint run
```

## What is different

One line of front matter:

```yaml
workspace: ../fixtures/small-repo
```

Before each run Checkpoint copies that directory into a throwaway one, starts
the agent **inside it**, and diffs the tree afterwards. `agent.py` opens
`src/app.py` by relative path and never learns it is being tested. The fixture
on disk is never written to, and each run starts from a fresh copy, so sixteen
gate runs are sixteen independent attempts.

This is a convention, not containment: the agent is pointed at a temporary
directory, and nothing stops a process from writing elsewhere on the machine.
Use a container for an agent you do not trust.

The tree is then queryable as `workspace.files`, keyed by `path`, so every root
the assertion language already had works on it:

```
count(created.workspace.files) == 1
exists(changed.workspace.files[path == "src/app.py"])
count(workspace.files[path == "src/app.py" && content ~ /^"""/]) == 1
count(changed.workspace.files[path == "pyproject.toml"]) == 0
```

## Write the criterion that can fail

`exists(workspace.files[path == "README.md"])` passes for an agent that did
nothing at all, because `README.md` was in the fixture before the agent
started. Criteria about the agent's *work* belong on `created`, `changed` and
`deleted` — which is why the scenario here says "Exactly 1 file was created"
rather than "a CHANGELOG exists". See
[docs/scenarios.md](../../docs/scenarios.md).

## The whole integration

```python
if __name__ == "__main__":
    print(run(os.environ["CHECKPOINT_TASK"]))
```

Same two lines as the other examples. `$CHECKPOINT_WORKSPACE` also holds the
path to the tree, for an agent that needs it spelled out or that sets its own
working directory.
