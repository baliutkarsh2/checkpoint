# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
