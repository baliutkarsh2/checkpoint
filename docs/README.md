# Checkpoint documentation

If you have five minutes, run `checkpoint demo` and then read
[Getting started](getting-started.md). Everything else here answers a question
you will have later.

| Page | What it answers |
|---|---|
| [Getting started](getting-started.md) | How do I point this at my agent and get a verdict? |
| [Scenarios](scenarios.md) | How do I write a test that means something — the file format, the criterion kinds, and the assertion language in full. |
| [Testing an agent that edits files](scenarios.md#testing-an-agent-that-edits-files) | Can I test a coding agent, a migration tool or a docs generator — one that changes a repository instead of calling an API? |
| [checkpoint.toml](configuration.md) | What can go in the config file — every setting, its default, and what it changes. |
| [Twins](twins.md) | What do the seven simulated services do, what do they seed with, how do I break them on purpose, and how do I add my own? |
| [The gate](gate.md) | What do SHIP and BLOCK mean, why sixteen runs, and how does CI use this? |
| [Troubleshooting](troubleshooting.md) | It did not do what I expected — what does this message mean and what do I do about it? |
| [Architecture](architecture.md) | What actually happens between `checkpoint run` and a score. |
| [Self-hosting](self-hosting.md) | How do I run the dashboard somewhere my team can see it? |
| [Releasing](releasing.md) | How does a version get published? |

The [example agents](../examples/) are working programs with their own
`checkpoint.toml` and scenarios; copy whichever is closer to your stack. Two
call services; [`coding-agent/`](../examples/coding-agent/) edits a file tree
and needs no API key to run.

Contributing? [CONTRIBUTING.md](../CONTRIBUTING.md), and the security model in
[SECURITY.md](../SECURITY.md).
