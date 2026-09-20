# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While Checkpoint is pre-1.0, a minor version may change behaviour. What is
covered by the version number, and what is not, is spelled out under
**Compatibility** below.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-09-20

First public release.

Checkpoint runs your agent — the command that already runs it, unmodified —
against stateful local copies of the services it calls, scores what it actually
changed in those services, and repeats until the pass rate means something.

### The agent under test

- Runs any command, in any language. The task arrives in `$CHECKPOINT_TASK`
  (or as an argument, or on stdin) and the final answer is read from stdout or
  `$CHECKPOINT_ANSWER_FILE`. An agent served over HTTP is addressed by `url`
  instead. There is no harness, wrapper or adapter to write.
- A TLS intercept proxy mints a per-run CA and routes production hostnames into
  the twins, so the code path under test — retries, pagination, error branches —
  is the one that ships. The CA expires after a day and is never added to any
  system trust store. The proxy sets the trust and proxy variables each runtime
  reads, so an unmodified client reaches the twins whatever the agent is written
  in; Python, Node and curl are covered end to end by the test suite.
- An egress policy (`none`, `llm`, `open`) bounds what else the agent can reach.
  Blocked connections are recorded and reported, never silently dropped.

### The services it runs against

- Stateful twins for **GitHub, Slack, Stripe, Linear, Supabase, Discord and
  Google Workspace**, each holding state across a multi-step run, each with a
  REST surface and an MCP server over the same state, and each shipping named
  seed datasets.
- Conformance suites drive every twin through the vendor's own SDK in CI, so a
  twin bug cannot quietly fail a correct agent.
- Fault injection: rate limits, permission denials, read-only mode, latency, a
  seeded error rate, or a targeted failure on one specific call.
- A project can register a twin of its own — any ASGI app — under `[twins.x]`
  and it behaves like a built-in.
- A `workspace:` scenario runs the agent inside a throwaway copy of a fixture
  directory and scores the diff, for agents that edit a repository rather than
  call an API.

### Scoring

- A scenario is one markdown file: the task, and what must be true afterwards.
- Criteria compile to assertions over what changed (`[D]`), what was called
  (`[T]`), or what the agent said (`[P]`, the only kind judged by a model on
  every run). `checkpoint check` shows how each one will be decided before you
  spend a run on it.
- An assertion language over the state diff and the call trace, with the
  compiled assertion stored alongside every result, so a verdict can be audited
  rather than trusted.
- A run that could not be scored is never counted as a pass or a failure.

### The verdict

- `checkpoint gate` runs each scenario N times and decides from the
  distribution: a Wilson confidence interval on the pass rate, not one lucky
  run. Verdicts are SHIP / BLOCK / CONDITIONAL / INCONCLUSIVE / ERROR, mapped to
  exit codes 0-4. Only SHIP exits 0.
- `pass^k` reports the chance that k runs in a row all pass, which is the
  question a release actually asks.
- Too few runs for SHIP to be reachable is INCONCLUSIVE, not a green build, and
  the output says how many runs it needs.
- Broken plumbing is never a verdict: a missing judge key, a sandbox that would
  not start, or a criterion that could not be evaluated is an ERROR.
- Pass rates are remembered per scenario and updated only on a SHIP, so a build
  that used to pass and now fails reads as a regression.
- `--report-only` prints the verdict and exits 0, for adopting the gate on an
  existing project before it blocks anything. It announces itself on every run
  and is deliberately not a `checkpoint.toml` setting.
- The runs of a scenario execute in parallel — four at once by default, or the
  CPU count if lower. Scenarios still run in sequence. The verdict cannot
  depend on the worker count, and a test pins that.

### Beyond the happy path

- `checkpoint redteam` — an adversarial pack covering all ten OWASP Agentic
  categories across all seven twins. Each attack is paired with a legitimate
  task, so refusing to work scores no better than falling for it. It reports
  which *class* of attack landed, and keeps "resisted" apart from "unproven".
- `checkpoint simulate` — a simulated user who argues, escalates and changes
  their mind over a multi-turn conversation.
- `checkpoint cert` / `report` — an Ed25519-signed, tamper-evident record of a
  verdict, and the assurance document a reviewer asks for.

### Using it

- `checkpoint demo` — offline, no API key, no network, in about three seconds.
- `checkpoint init` — writes a config, a starter scenario and a `.gitignore`
  line, and adapts to the repository it finds: a CI workflow where there is a
  `.github/`, a coding-agent skill where there is a `.claude/`. The starter
  scenario is all assertions, so the first run needs no API key.
- `checkpoint doctor` — checks the machine by starting a twin and self-testing
  the proxy, rather than taking your word for it.
- A GitHub Action, a pytest plugin, an importable Python API, an MCP server, and
  a local dashboard over past runs.
- Judging works with any `gpt-*`, `claude-*` or `gemini-*` model, or any
  OpenAI-compatible endpoint via `CHECKPOINT_LLM_BASE_URL`.

### Compatibility

Until 1.0, these are covered by the version number and will not change without a
minor bump and a note here: the scenario file format, the criterion syntax and
assertion language, the gate's exit codes, the `checkpoint.toml` schema, and the
public Python API exported from `checkpoint`.

These are not yet stable: the run-record and certificate JSON shapes, the twins'
internal state layout, the dashboard's HTTP API, and any module not re-exported
from the top-level package.

### Security

- Both CI security gates are blocking: secret scanning over the full history,
  and CodeQL.
- A tripwire in the ordinary test suite fails the build on a real-format
  provider key in any tracked file — OpenAI, Anthropic, Google, AWS, GitHub,
  Slack, Stripe, GitLab, Hugging Face and PEM private keys.
- The twins' bootstrap credentials are synthetic by construction: each keeps the
  prefix its SDK requires and carries a literal marker in its body.
- See [SECURITY.md](SECURITY.md) for the boundaries this tool does and does not
  provide.

[Unreleased]: https://github.com/baliutkarsh2/checkpoint/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/baliutkarsh2/checkpoint/releases/tag/v0.1.0
