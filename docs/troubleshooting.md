# Troubleshooting

The failures people actually hit, what each one means, and the fix. If something
here is wrong or missing, that is a bug worth reporting.

Start with `checkpoint doctor`. It checks Python, mints a CA and self-tests the
intercept proxy, starts a twin and talks to it, and reports what your project
config and judge model resolve to — so it distinguishes "this machine cannot run
Checkpoint" from "this scenario is wrong" before you spend a run finding out.

---

## The agent will not start

### `agent command not found: 'pythn' (is it installed and on PATH?)`

A typo, or an interpreter that is not on `PATH` in the environment the run uses.
Check with the same shell: `pythn --version`.

### `could not start agent: [WinError 193] %1 is not a valid Win32 application`

On Windows, a script named without its interpreter. `--command "my_agent.py"`
asks the OS to execute the file directly; it is not an executable.

```bash
checkpoint init --command "python my_agent.py"   # not "my_agent.py"
```

### `not allowed to run '...'`

The file is not executable, or the path is a directory.

### The agent starts but Checkpoint reads no answer

Checkpoint takes the final answer from stdout. If your agent logs progress to
stdout as well, the last thing printed is not the answer — write the answer to
`$CHECKPOINT_ANSWER_FILE` instead and Checkpoint will prefer it.

---

## The agent ran, but nothing happened

### `The agent made no calls to the sandboxed services (github)`

The run completed and the twin was never touched, so every `[D]` criterion about
created or changed state fails. Three usual causes:

1. **The HTTP client ignores the proxy.** Checkpoint sets `HTTPS_PROXY` and
   `https_proxy` and the CA variables for every common runtime. A client
   configured with an explicit `proxies=None`, or one that pins its own CA
   bundle, bypasses all of it. Let the client read the environment.
2. **The agent talks to a hard-coded localhost URL** rather than the production
   hostname. Either point it at the production hostname and let the intercept
   do its job, or read `CHECKPOINT_<TWIN>_URL` — `CHECKPOINT_GITHUB_URL` and so
   on are set for every twin in the run.
3. **The agent short-circuited.** Check its own output with
   `checkpoint run --verbose`.

### `Blocked 3 connection(s) to api.example.com (outside the sandbox)`

The egress policy stopped a host that is not a twin and not a known model
provider. That is the default (`egress = "llm"`) working. If the agent genuinely
needs that host:

```bash
checkpoint run --allow-host api.example.com
```

or `allow_hosts = ["api.example.com"]` under `[sandbox]`. `--egress open`
removes the boundary entirely; see [SECURITY.md](../SECURITY.md) before you do.

---

## Scoring

### `No API key for model 'gpt-5.6-luna': set OPENAI_API_KEY ...`

Only `[P]` criteria need a judge on every run, and a `[D]`/`[T]` that no pattern
recognises needs one the first time. The message names three ways out: set the
key, pass `--model` for a provider you do have, or point
`CHECKPOINT_LLM_BASE_URL` at any OpenAI-compatible endpoint.

To need none at all, make every criterion an assertion. `checkpoint check` tells
you where you stand before you run anything — a scenario that reports *"ready,
every check deterministic"* costs nothing and works offline.

### The run says `80/100` and exits 2

The score is over the criteria that could be *scored*; the exit code reflects
that not all of them could be. A criterion that could not be evaluated is not
evidence the agent failed it, so it leaves the score entirely — and the run
line says `1 not scored` when that happens. Fix the reason it could not be
scored (usually a missing judge key) and the run becomes decisive either way.

### A criterion passes for an agent that did nothing

This is the mistake worth hunting. "An issue exists" can already be true of the
seed; "exactly one issue was created" cannot. Run `checkpoint check`: it prints
the assertion each criterion compiles to, which is where a vacuous one shows
itself. Prefer `count(created.<twin>.<collection>) == 1` over `exists(...)`.

### `github.issues[...] matched 0 items` on a criterion that reads a field

Reading a field off a selection needs exactly one match, so a selection that
matched nothing is an *error* — "we could not score this" — not a failure. Count
a guard instead:

```markdown
- [D] The issue is still open
  =>  count(github.issues[title == "Login broken" && state == "open"]) == 1
```

---

## The gate

### `INCONCLUSIVE`, exit 3

Too few runs for SHIP to be reachable at your thresholds — at the defaults, a
flawless 5/5 still cannot clear the bar, because five successes are not yet
evidence. This is deliberately not a soft pass. The output says how many runs it
needs; give it that many, or lower `ship_min`.

### `CONDITIONAL`, exit 2

Enough runs to decide, and the answer is genuinely in between. Only SHIP exits
0. `--allow-conditional` opts into a green CONDITIONAL, and `--strict` refuses
it even then.

### `ERROR`, exit 4

No verdict is possible: the sandbox, the judge, or the scenarios broke. This is
never a statement about your agent, and the gate will not invent one. The
message says which.

### The gate is slow

Each scenario runs N times. The runs of one scenario execute in parallel — four
at once by default, or the CPU count if lower — but scenarios run one after
another. Raise it with `-j`, or cut `runs` while iterating and keep the full
count for the build that decides.

### Every build reads as a first run, and a regression is never reported

Baselines live in `.checkpoint/baselines.json`, which `checkpoint init` adds to
`.gitignore`. Restore that directory from your CI cache between builds, or the
gate has nothing to compare against. Pass rates update only on a SHIP.

---

## Twins

### `the github twin is not running`

`checkpoint twins tools` and the other commands that ask a twin something need
one already started with `checkpoint twins start github`. A scenario run starts
and stops its own twins, so this only applies when you drive them by hand.

### `the github twin is registered but its process is gone`

A twin's process died, or the machine restarted, and the record outlived it.
`checkpoint twins stop github` clears the record; start it again.

### The twin does not answer a call my agent makes

The twins reproduce the endpoints scenarios exercise, not every corner of every
API. `checkpoint twins list` is the inventory, and `tests/sdk/` is the evidence
of what is checked against each vendor's own SDK. A missing endpoint is a
legitimate bug report.

---

## Still stuck

- `checkpoint run --verbose` streams the agent's own output while it runs.
- `checkpoint runs show <id>` replays what a past run did, and
  `checkpoint runs trace <id>` lists every call it made.
- `checkpoint run --explain` asks the judge why each failed criterion failed.
- `checkpoint view` opens the same evidence in a browser.

If it still makes no sense,
[open an issue](https://github.com/baliutkarsh2/checkpoint/issues) with the
output of `checkpoint doctor`.
