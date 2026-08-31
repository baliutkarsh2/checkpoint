# Contributing to Checkpoint

Thanks for helping build the release gate for AI agents. Checkpoint is open source
under Apache-2.0; contributions of scenarios, twin coverage, evaluators, and fixes
are all welcome.

## Dev setup

```bash
git clone https://github.com/Aaditya2605/checkpoint
cd checkpoint
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
checkpoint doctor
```

Node 20+ is needed only if you touch the dashboard SPA.

## Running the tests

```bash
pytest -q                      # full suite
pytest tests/twins -q          # just the twins
pytest tests/test_no_tracked_secrets.py -q   # the secret tripwire
```

The suite runs offline: LLM calls are behind injectable client factories, and each
test isolates state via `tmp_path` / `CHECKPOINT_HOME`. Docker-mode tests mock the
docker client, so a running daemon is not required to run the suite.

## Changing the dashboard

The SPA source lives in `checkpoint/dashboard/web`; the built bundle is committed to
`checkpoint/dashboard/static` and shipped in the wheel. If you edit the SPA you must
rebuild and commit the bundle, or CI fails the drift check:

```bash
cd checkpoint/dashboard/web && npm ci && npm run build
```

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
