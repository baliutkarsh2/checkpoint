# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **One config file: `checkpoint.toml`.** Settings used to live in
  `.checkpoint.json`, `harness.json` and `~/.checkpoint/config.json`, each read
  by a different command — and an audit found keys in them that nothing read at
  all. There is now one file, found by walking up from the working directory,
  with one precedence rule everywhere: flag > environment > file > default.
  Unknown keys and wrong types are errors rather than silence. `checkpoint init`
  writes it; the three old files, and the modules behind them, are gone.
- **The CLI is 15 commands instead of ~40.** `checkpoint --help` opened with an
  alphabetical wall of commands, several of which were aliases of each other
  (`clone status` invoked `clone inspect`; `debug inspect` invoked
  `traces detail`). The surface is now grouped by what you are trying to do, and
  each command lives in its own module, loaded only when it runs. Renames:
  `validate`→`check`, `serve`→`view`, `clone`→`twins`, `compliance`→`report`,
  `scenario generate`→`new`, `gen-attacks`→`redteam generate`,
  `redteam-mcp`→`redteam serve-poisoned`, and `traces`/`replay`/`compare`/
  `report`/`db`/`debug export` all folded into `runs`. Removed: `config` and
  `whoami` (superseded by `checkpoint.toml` and `doctor`), `badge`, `ci init`
  (now `init --ci`), `scenario list`, and the `--harness` flag (now `--command`,
  and usually unnecessary).
- **`checkpoint run` takes 19 options instead of 33**, and `--pass-threshold` is
  gone from it: deciding whether a build ships from one run is what `gate`
  exists to prevent.
- **Red-team runs default to 16, not 5.** At five runs a clean sweep cannot
  clear the confidence bar, so *every* attack was reported as a vulnerability
  and labelled "flaky (attack lands sometimes)" — which is false for 5/5. The
  report now separates "resisted" from "nothing landed, but the evidence cannot
  prove it", and exits 2 rather than 1 for the latter. `run_redteam` also
  accepts the resolved agent and sandbox options, which it previously dropped.
- **`checkpoint doctor` no longer fails when Docker is absent**, and its checks
  earn their place: it starts a twin and reads its state back, and self-tests the
  intercept proxy, rather than probing ports nothing binds.
- `checkpoint/clone_manager.py` is now `checkpoint/twins/sessions.py`, and it
  resolves twins through the registry per call, so a twin declared in
  `checkpoint.toml` can be started like a built-in one.

### Added

- **Your own twins.** `[twins.<name>]` in `checkpoint.toml` points Checkpoint at
  any ASGI app in your repository; it is then a twin like the built-in seven —
  scenarios name it, the sandbox starts it, the proxy routes its production
  hostnames into it.
- **`checkpoint check` shows the assertion behind every criterion** before a run
  is spent, which is where a criterion an idle agent would pass gives itself
  away.
- **The pytest plugin runs scenarios.** `checkpoint_run("scenarios/refund.md")`
  returns a scored `RunResult`, and `checkpoint_sandbox` hands a test the real
  `Sandbox`. The old `checkpoint_twin` fixture and its `TwinHandle` are gone.
- `checkpoint.toml`'s `[twins]`, and a public API on the package itself:
  `from checkpoint import Agent, RunOptions, Sandbox, parse_file, run_scenario`.
  Names resolve on first use, so `import checkpoint` stays cheap.

### Removed

- **Docker run mode**, in full: `checkpoint/docker/`, the TLS sidecar image, the
  generated harness images, `--docker`, `--docker-logs`, `--harness-dir` and the
  `docker` dependency. It existed because interception used to require a
  container; interception now runs in-process on any platform, and the container
  path had become a second, worse copy of the engine — it honoured neither the
  fault injection nor the egress policy, and its hard-coded domain table had
  drifted from the twin registry, so a Google Workspace agent's token refresh
  went to the real Google. Container isolation, if it returns, belongs on the
  engine rather than beside it. The repository's own `Dockerfile`,
  `docker-compose.yml`, `fly.toml` and `render.yaml` are unaffected: they deploy
  the dashboard.
- `checkpoint/sdk.py`. The package itself is the API now, in the same vocabulary
  the CLI uses, rather than a second one with its own names for the same things.

### Fixed

- **A must-pass criterion was not enforced by the gate.** `[D!]` was honored
  only by `checkpoint run`; the gate scored each run on its average, so an agent
  that deleted what a scenario said never to delete could outscore the breach
  and ship. A run that breaks one now scores zero for that run, and the gate
  names the criterion.
- **Existence checks counted zero on three twins.** The live-record filter
  tested `tombstone == null`, but Slack, Stripe and Google Workspace write
  `false` on a live record — so "no more than 2 messages exist" passed against a
  four-message seed, and every "N records exist" criterion on those twins was
  checking nothing. It tests the mark for truthiness now, as the delta roots do.
- **Supabase auth users and storage buckets were unaddressable.** Their
  collections were named with dots, which the assertion language reads as
  another path level, so every criterion about them failed as a schema error
  rather than being evaluated. The views are `auth_users`, `storage_buckets` and
  `storage_objects`.
- **Three intercepted hostnames reached their twins with no credential.** The
  proxy kept its own copy of the domain table and it had drifted from the
  registry — `uploads.github.com`, `discordapp.com` and `oauth2.googleapis.com`
  were missing, so a request to any of them arrived unauthenticated and was
  refused. It reads the registry now.
- **A twin's MCP surface answered 421 to intercepted requests.** The MCP
  server's DNS-rebinding guard only accepts a localhost `Host` header, and an
  intercepted request carries the production hostname — so the agents that
  needed no modification were exactly the ones it turned away.
- **`checkpoint init` wrote invalid TOML** for any command containing a Windows
  path or a quote: it escaped the value with `repr`, whose rules are not TOML's.
- **"The most recent N runs" was ordered by file mtime**, which ties for runs
  that finish in the same second, so `runs trend -n 2` could answer about the
  wrong two.
- **An assurance report graded APPROVED with no adversarial testing at all**,
  under an empty OWASP table that reads as "nothing found". Zero attacks is
  CONDITIONAL now, and the section says that nobody looked.
- **Stacked pull requests ran no CI.** The workflows filtered `pull_request` on
  `[main, master]`, so a branch merging into another feature branch reached main
  having never been tested; only the final merge ran anything.
- **`twins: [github]` in YAML front matter started no twins.** The engine
  re-parsed the setting with `str(...).split(",")`, so a YAML list arrived as the
  literal string `"['github']"` and the run failed to set up — the documented
  front-matter syntax, and the one the starter scenario uses.
- **Assertions were being eaten by the terminal renderer.** Rich reads `[...]`
  as a style tag, so `github.issues[title == "x"]` printed as `github.issues`,
  quietly showing a precise check as a vague one.
- **`schema_for` described every collection as having no fields**, because it
  read them from a twin at rest, which holds nothing. It now pools fields across
  the twin's bundled seeds, so `checkpoint check` and the scenario generator
  know that an issue has a `title` and a `state`.
- `replay --clone` matched nothing: trace events carry a `twin` key, and the
  filter looked for `clone`.

- **The Docker sidecar no longer uses mitmproxy.** TLS interception is now done
  by Checkpoint's own proxy (`checkpoint/proxy/`, `python -m checkpoint.proxy`):
  forward-proxy (CONNECT) and transparent (SNI) modes, per-host certificates
  from a per-run CA, an egress allowlist for non-routed hosts, and an event log
  of every request. mitmproxy pinned exact `h11`/`h2` versions and capped
  `typing-extensions`, so installing it silently downgraded other packages; the
  `proxy` extra is gone and the proxy's own dependencies (`h11`,
  `cryptography`, `certifi`) are core. `checkpoint doctor` now self-tests the
  proxy instead of checking that mitmproxy imports.
- **The gate is safe by default: only SHIP exits 0.** CONDITIONAL used to exit 0,
  so 0/3 passing runs (Wilson upper bound 0.56 > `block_max`) — and 9/20 at the
  default N — produced a green build. Exit codes are now SHIP 0, BLOCK 1,
  CONDITIONAL 2, INCONCLUSIVE 3, ERROR 4. `--allow-conditional` opts back into a
  green CONDITIONAL; `--strict` refuses it even then.
- **A new INCONCLUSIVE verdict** for evidence that cannot decide. A flawless 5/5
  cannot clear `ship_min 0.80` at any confidence, so it is no longer labelled
  "flaky": the gate reports how many clean runs SHIP needs
  (`ceil(ship_min·z²/(1−ship_min))`, 16 by default) and exits non-zero.
- **Infrastructure failures are first class.** A sandbox that will not start, a
  missing judge credential, or a scenario that cannot be parsed is reported as
  ERROR, kept out of the pass-rate sample, and exits non-zero — never scored as a
  run the agent lost. A missing judge key is caught before any run happens.
- **Only real scenarios run.** Files under the gate target with no task or no
  success criteria (a `README.md`, say) are reported as skipped with a reason
  instead of being run with an empty task; a target matching nothing is an ERROR.
- **Honest baselines.** Keyed by the scenario's path relative to the gate target
  plus a hash of its success criteria, stored under a path-independent key so CI
  checkouts find them, and updated only on a SHIP — a flaky run can no longer
  ratchet its own bar down. A regression now requires a statistically meaningful
  drop (the baseline must fall outside the current interval), not just a
  difference of point estimates.
- **Exact statistics.** The z-value comes from `NormalDist().inv_cdf`, so
  `--confidence 0.85` no longer silently uses the 0.80 value, and threshold
  comparisons are tolerance-safe (a 0.20 drop that floats to 0.1999… still counts).

## [0.1.0] - 2026-08-31

First public release.

### Added

- **Statistical release gate** — `checkpoint gate` runs each scenario N times and
  decides from the distribution of outcomes (Wilson confidence interval on the
  pass rate), emitting SHIP / CONDITIONAL / BLOCK with a CI exit code. Per-scenario
  results are classified `stable_pass` / `flaky` / `stable_fail` / `regression`
  against a persisted baseline.
- **pass^k reliability** — the unbiased tau-bench estimator `C(passes,k)/C(n,k)`,
  reported in the gate table, `-o json`, and the signed certificate, so a
  90%-pass agent reads honestly as `pass^10 ~= 0.35`.
- **`checkpoint demo`** — a zero-setup proof: one command, no Docker, no API key,
  scoring 100/100 offline against a bundled agent and scenario.
- **Stateful twins** for GitHub, Slack, Stripe, Linear, Supabase, Discord and
  Google Workspace, with REST + MCP surfaces, named seeds, and fault-injection
  knobs. A mitmproxy TLS sidecar routes an unmodified agent's real SDK calls to
  them in Docker mode.
- **Three-stage evaluator** (deterministic catalog -> schema-validated LLM parse
  -> LLM judge) plus trajectory-level `[T]` criteria scored from the API-call
  sequence.
- **Signed Trust Certificates** (Ed25519) and an **Agent Assurance Report** with
  OWASP Agentic / NIST AI RMF / EU AI Act cross-references.
- **Red-teaming** (`checkpoint redteam`, `gen-attacks`, `redteam-mcp`) mapped to
  the OWASP Agentic catalog, and **simulated multi-turn users**
  (`checkpoint simulate`) with a plausibility signal.
- **GitHub Action** for gating an agent in CI, exercised end to end by this
  repo's own CI.
- **Web dashboard** (`checkpoint serve`) with failure-first run inspection, live
  SSE streaming, run comparison, and twin management.
- **MCP server** (`checkpoint mcp`) so a coding agent can gate the agent it is
  writing, plus a SQLite run store and OpenTelemetry GenAI trace ingestion.

### Security

- Both CI security gates are blocking: gitleaks over full history and CodeQL.
- A secret tripwire in the test suite fails the build on a real-format provider
  key in any tracked file, covering OpenAI, Anthropic, Google, AWS, GitHub,
  Slack, Stripe, GitLab, Hugging Face and PEM private keys.
- Published from a history rewritten to remove a previously-committed `.env`.

[Unreleased]: https://github.com/baliutkarsh2/checkpoint/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/baliutkarsh2/checkpoint/releases/tag/v0.1.0
