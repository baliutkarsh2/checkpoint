# Releasing

How a version gets published, and the one-time steps that have to happen before
the first one does. Several need repository-admin or PyPI account access.

## Publishing a version

`.github/workflows/release.yml` publishes to PyPI through trusted publishing
(OIDC), so no API token is stored anywhere. A push of a `v*` tag runs it:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow, in order:

1. Builds the dashboard SPA from source on Node 22. The bundle is gitignored,
   so it has to be built here for `python -m build` to pick it up as package
   data.
2. Checks the tag against `checkpoint.__version__` and fails if they differ. A
   tag must publish the version it names; without this, `git tag v0.2.0` would
   ship whatever version the package happened to declare.
3. Runs the full test suite **on the tagged commit**, so nothing is published
   whose tests were only ever run somewhere else.
4. Builds the sdist and the wheel, and runs `twine check`.
5. Verifies the wheel contains every asset read at runtime, and that it ships
   no bytecode. A missing asset fails the release rather than becoming a broken
   `pip install`.
6. Publishes, with PEP 740 attestations: signed proof the artifact came from
   this workflow in this repository, which an installer can verify.

The publish job runs only for a `v*` tag. `workflow_dispatch` builds and tests
but never publishes, so a manual run on a branch cannot ship that branch under
a real version number.

## One-time: PyPI

The distribution is **`checkpoint-agents`**. `checkpoint` on PyPI is unrelated
software, which is why `tests/test_install_instructions.py` fails the build if
any tracked file tells someone to install it.

1. Create or take ownership of the `checkpoint-agents` project on
   <https://pypi.org>.
2. Project → *Publishing* → add a trusted publisher: owner `baliutkarsh2`,
   repository `checkpoint`, workflow `release.yml`, environment `pypi`. For the
   very first release this is a *pending publisher*, since the project does not
   exist yet.
3. Optionally dry-run against TestPyPI first with a `v*rc*` tag.

Once a release is on PyPI, switch the documented install from git to the
distribution: both `pip install git+https://github.com/baliutkarsh2/checkpoint`
lines in `README.md`, and the "PyPI release pending" note beside them. The
GitHub Action already prefers the published distribution and falls back to a
source install, so it needs no change.

## One-time: going public

**Rotate the previously leaked key first.** A live-format OpenAI key was
readable in the predecessor repository's history. This repository's history is
clean, but a history rewrite cannot un-share a secret that was already
published: revoke it in the OpenAI dashboard and issue a replacement that lives
only in a git-ignored `.env`. See [SECURITY.md](../SECURITY.md).

Then verify this history is clean before flipping visibility:

```bash
git log --all --oneline -- .env                    # expect no output
git rev-list --all --objects | grep -E '\s\.env$'  # expect no output
git log --all --oneline -S'sk-proj-'               # only docs and test patterns
```

The last command legitimately matches `SECURITY.md`, this file and
`tests/test_no_tracked_secrets.py`, which contain the *pattern* rather than a
key.

After making the repository public (Settings → General → Danger Zone), turn on
the free public-repo protections under Settings → Code security:

- **Secret scanning** — catches a committed credential after the fact.
- **Push protection** — blocks the commit that would leak one, which is what
  prevents a repeat.

Both security workflows already fail the build rather than reporting and
continuing: gitleaks, and CodeQL (code scanning is free on a public repo).
`gh api repos/<owner>/<repo>/code-scanning/alerts` returning 403 means code
scanning is off. Neither is load-bearing on its own —
`tests/test_no_tracked_secrets.py` runs in the gating test job and fails on a
real provider key in any tracked file.

## One-time: branch protection

Require these checks on `main`:

- `Build SPA + run pytest + verify wheel`
- `Validate the GitHub Action`

The other CI jobs — the pytest matrix, twin conformance against the official
SDKs, the dashboard image build, ruff, and the gate against the bundled agent —
are worth watching but are covered by the two above for merge purposes.

Then finish the public face: repository description and topics, and Discussions
enabled, which the issue-template chooser already links to.
