# Architecture

What happens between `checkpoint run` and a score.

```
checkpoint.toml ──► Project ──► Agent ──┐
scenarios/*.md  ──► Scenario ───────────┤
                                        ▼
                             Sandbox: twins + intercept proxy + egress policy
                                        │
                        the agent runs, the twins remember
                                        ▼
                    seed state · final state · trace · egress · answer
                                        ▼
                    criteria ──► assertions      (state and calls)
                             └──► judge model    ([P] only)
                                        ▼
                                    RunResult
```

## Project

`Project.load()` walks up from the working directory for the nearest
`checkpoint.toml`, at most six levels. Unknown sections, unknown keys and wrong
types are errors, because a setting that looks applied and is not is worse than
no setting at all. One precedence rule holds everywhere:

```
command-line flag  >  environment variable  >  checkpoint.toml  >  default
```

There is one config file. Settings used to be spread over three, each read by a
different command, and several documented keys were read by nothing.

## Agent

An agent is a black box Checkpoint can start: a command in any language, or an
HTTP endpoint. Checkpoint never imports it, wraps it or edits it.

A command agent is started as a subprocess with the sandbox's environment. It
receives the task in `$CHECKPOINT_TASK`, as an argument, or on stdin
(`task_via`), and reports its answer on stdout or in `$CHECKPOINT_ANSWER_FILE`.
On timeout the whole process tree is killed, not just the parent: agents spawn
node CLIs, browsers and tool servers, and a survivor keeps mutating the sandbox
after the run it belonged to has ended.

On Windows the command is split with the C runtime's rules rather than POSIX
ones, and a bare program name is resolved against the *agent's* `PATH` — without
that, `python my_agent.py` runs Checkpoint's interpreter instead of the agent's
virtualenv.

## Sandbox

A sandbox is the simulated world one run happens in.

**Twins.** Every twin in the run is served from one child process, each on a
free loopback port. A twin keeps a service's state in memory and speaks the
real API, so the agent's calls change state and the next call sees the change.

**The intercept proxy.** With `intercept = true` (the default) a certificate
authority is minted for this run and an intercepting proxy starts. The agent's
environment points `HTTPS_PROXY` at it and points every trust store Checkpoint
knows about at a bundle of the public roots *plus* this CA — `SSL_CERT_FILE`
for OpenSSL stacks, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `HTTPLIB2_CA_CERTS`
for the Google clients, `NODE_EXTRA_CA_CERTS` for Node. The bundle rather than
the CA alone, so the agent's own non-intercepted HTTPS keeps verifying against
the real roots.

For a hostname that belongs to a twin, the proxy terminates TLS with a
certificate from that CA, replays the request against the twin, and stamps in
the twin's credential. The agent's SDK is unmodified and its base URL is still
`https://api.github.com`. That is the point: the code path under test is the
one that ships. Injecting a test base URL instead would test a branch that only
exists in tests.

The CA expires after a day and only the agent under test ever trusts it.

**The egress policy.** Everything that is not a twin hostname is decided by the
egress setting: `open` allows it, `llm` allows the model providers plus
`allow_hosts`, `none` allows only `allow_hosts`. Allowed traffic gets a blind
TCP tunnel that is never decrypted; denied traffic gets a 403, and either way
the connection is recorded, so `count(egress[allowed == false]) == 0` is a
criterion you can write.

**Preparation.** `prepare()` resets every twin and then applies the scenario's
seed and fault config. A gate builds one sandbox per scenario and reuses it
across all sixteen runs — starting seven servers sixteen times is slow — and
resets it between them, so each run still starts from exactly the seed.

## Scoring

When the agent exits, the sandbox is read: each twin's collections before and
after, the ordered trace of every call, and the egress log. Those become the
*world* an assertion is evaluated against, along with the answer, the task, the
exit code and the duration.

Each criterion takes one of two routes:

- **An assertion** for anything that is a claim about the world: pinned in the
  scenario after `=>`, matched from a known phrasing, or translated once by a
  model and cached in `.checkpoint/cache/assertions.json`. A model is good at
  translating English into an expression and bad at being a stable verdict, so
  what it produces is the translation — which is validated against the twins'
  schema, shown to the author, stored with the run, and reused.
- **The judge model** for `[P]`, where the question is genuinely about what the
  agent said.

Evaluation is tri-state. An assertion that cannot be evaluated — a field that
does not exist, a selection that matched two things where one was meant — is an
error, and the run carries no verdict rather than a wrong one.

## RunResult

One record per run: the score, every criterion with how it was decided and why,
the trace, the twins' state before and after, the egress log, the agent's
stdout and stderr, and any warnings. It is written to
`.checkpoint/cache/runs/<id>.json` and is what `checkpoint runs show`,
`checkpoint view` and the certificate are all built from.

## The gate

`checkpoint gate` runs the loop above N times per scenario and turns the
resulting pass rates into one verdict, with baselines in
`.checkpoint/baselines.json`. That layer is its own page:
[The gate](gate.md).

## Where things live

| Path | What |
|---|---|
| `checkpoint/cli/` | Fifteen commands, each in its own module, imported only when it runs |
| `checkpoint/project.py` | `checkpoint.toml` |
| `checkpoint/engine/` | `Agent`, `Sandbox`, `run_scenario` |
| `checkpoint/proxy/` | The intercepting proxy, its CA and the egress policy |
| `checkpoint/twins/` | One module per twin, plus `kit.py` — the shared control plane and fault model — and `registry.py` |
| `checkpoint/scenario.py` | The scenario file format |
| `checkpoint/eval/` | The assertion language, the compiler, the judge |
| `checkpoint/gate/` | Policy, statistics, baselines, certificates |
| `checkpoint/dashboard/` | The `checkpoint view` server and its SPA |
