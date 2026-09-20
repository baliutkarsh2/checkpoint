# Contributing to Checkpoint

Thanks for helping build the release gate for AI agents. Checkpoint is open source
under Apache-2.0; contributions of scenarios, twin coverage, evaluators, and fixes
are all welcome.

## Dev setup

```bash
git clone https://github.com/baliutkarsh2/checkpoint
cd checkpoint
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
checkpoint doctor
```

Node 22+ is needed to build the dashboard SPA (the bundler ships native bindings
built for current Node). The built bundle is committed, so this is only needed
if you change the SPA:

```bash
cd checkpoint/dashboard/web && npm ci && npm run build
```

Skip this if you never run the dashboard; the CLI works without it.

## Linting

```bash
ruff check checkpoint/ tests/      # what CI enforces
ruff check --fix checkpoint/ tests/
```

`ruff` ships in the `dev` extra. The rule set is curated for signal over style
(real bugs, import hygiene, likely defects, modern-Python upgrades); line length
is deliberately not enforced. Optionally run the same gate before each commit:

```bash
pip install pre-commit && pre-commit install
```

## Running the tests

```bash
pytest -q                      # full suite
pytest tests/twins -q          # just the twins
pytest -q --cov                # with a coverage report
pytest -q -m "not integration" # skip tests that spawn twin subprocesses
```

Markers (`slow`, `integration`, `sdk`) are declared in `pyproject.toml` and
enforced with `--strict-markers`, so a typo fails rather than silently matching
nothing. Every test is bounded by a 300s timeout.

The suite runs offline and needs no API key: LLM calls go through injectable
client factories, and each test isolates its state under `tmp_path` or
`CHECKPOINT_HOME`. A test that would reach the network is a bug — the whole
point of this project is that you should not have to.

## Changing the dashboard

The SPA source lives in `checkpoint/dashboard/web`. The built bundle
(`checkpoint/dashboard/static/`) **is committed**, so a plain `pip install` from
git gives users a working dashboard with no Node required. If you change the SPA
you must rebuild and commit the bundle — CI rebuilds it and fails if the
committed copy is stale:

```bash
cd checkpoint/dashboard/web && npm ci && npm run build   # outputs ../static
checkpoint view                                           # serves the fresh bundle
```

## Adding a twin or scenario

- **Twins** live in `checkpoint/twins/` with a matching MCP wrapper in
  `checkpoint/mcp_servers/`. Keep the wire shape faithful to the real SDK and add
  seeds + tests under `tests/twins/`.
- **Scenarios** are markdown under `scenarios/`: front matter, `## Task`,
  `## Criteria`. Run `checkpoint check` before submitting — it prints the
  assertion behind every criterion, which is where a criterion that an idle
  agent would pass gives itself away.

## Ground rules

- **Never commit real credentials.** Synthetic twin tokens live only in
  `checkpoint/fake_credentials.py` and carry the `CHECKPOINTFAKE` marker. The secret
  tripwire (`tests/test_no_tracked_secrets.py`) and the gitleaks CI job enforce this.
- Line endings are normalized by `.gitattributes` (shell scripts stay LF).
- Keep the README honest. `tests/test_readme.py` checks that every command it
  names exists and that retired vocabulary stays retired; it cannot check that a
  claim is true, so that part is on you. Document what ships, and label a
  roadmap item as one.
- Open an issue before a large change so we can align on approach.

## Reporting security issues

See `SECURITY.md`. Please do not open public issues for vulnerabilities.

## Code of conduct

Participation in this project is governed by the [Contributor Covenant](./CODE_OF_CONDUCT.md).
Report unacceptable behavior to hello@usecheckpoint.dev.
