# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The bundled scenarios cannot be passed by an agent that does nothing.**
  Twelve of the seventeen scenarios under `scenarios/` scored 100/100 for an
  agent that made no calls at all, because their criteria counted absolute state
  ("at least one issue exists in `acme/webapp`" is true of the seed) instead of
  what the agent changed. Every scenario now states its checks against
  `created.`/`deleted.`/`changed.`, marks its "do no harm" checks `[D!]`/`[T!]`,
  uses YAML front matter with `## Task`/`## Criteria`, and describes the state
  its seed really loads. Criteria that could never pass — a label no seed
  contains, a Linear issue that is not there, a mailbox head count the seed
  already exceeds — are gone. Each adversarial scenario now also asks for a safe
  alternative, so refusing the destructive part is necessary but not sufficient.
  `tests/test_bundled_scenarios.py` runs every scenario twice in CI: once with a
  do-nothing agent, which must score below 100 while every must-pass safety
  criterion still holds, and once with a scripted agent that really does the
  work, which must score 100.
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
