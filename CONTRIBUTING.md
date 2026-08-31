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
built for current Node). The built bundle is not committed — build it once so
`checkpoint serve` has a dashboard to serve from a source checkout:

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

Markers (`slow`, `integration`, `docker`) are declared in `pyproject.toml` and
enforced with `--strict-markers`, so a typo fails rather than silently matching
nothing. Every test is bounded by a 300s timeout.

The suite runs offline: LLM calls are behind injectable client factories, and each
test isolates state via `tmp_path` / `CHECKPOINT_HOME`. Docker-mode tests mock the
docker client, so a running daemon is not required to run the suite.

## Changing the dashboard

The SPA source lives in `checkpoint/dashboard/web`. The built bundle
(`checkpoint/dashboard/static/`) is **not** committed — it is gitignored and built
fresh in CI, then packaged into the wheel. Build it locally to preview your changes:

```bash
cd checkpoint/dashboard/web && npm ci && npm run build   # outputs ../static
checkpoint serve                                          # serves the fresh bundle
```

CI (and the release workflow) run this same build on Node 22, so what ships is
always a clean build from source — there is no committed artifact to keep in sync.

## Adding a twin or scenario

- **Twins** live in `checkpoint/twins/` with a matching MCP wrapper in
  `checkpoint/mcp_servers/`. Keep the wire shape faithful to the real SDK and add
  seeds + tests under `tests/twins/`.
- **Scenarios** are markdown under `scenarios/` (`## Setup`, `## Prompt`,
  `## Success Criteria`, `## Config`). Prefer `[D]` criteria the deterministic
  catalog can check; run `checkpoint validate` before submitting.

## Ground rules

- **Never commit real credentials.** Synthetic twin tokens live only in
  `checkpoint/fake_credentials.py` and carry the `CHECKPOINTFAKE` marker. The secret
  tripwire (`tests/test_no_tracked_secrets.py`) and the gitleaks CI job enforce this.
- Line endings are normalized by `.gitattributes` (shell scripts stay LF).
- Keep `README.md` under 200 lines and honest — document what ships, label roadmap
  items as roadmap.
- Open an issue before a large change so we can align on approach.

## Reporting security issues

See `SECURITY.md`. Please do not open public issues for vulnerabilities.

## Code of conduct

Participation in this project is governed by the [Contributor Covenant](./CODE_OF_CONDUCT.md).
Report unacceptable behavior to hello@usecheckpoint.dev.

## Security

Please do not open public issues for vulnerabilities — see [SECURITY.md](./SECURITY.md)
for the private reporting channel.
