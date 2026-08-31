# Releasing and going public

The runbook for taking Checkpoint from a private repo to a published, public
project. Steps are ordered; several need repository-admin or account access.

---

## 1. Rotate the leaked key — **do this first**

A `.env` containing a live-format OpenAI key (`sk-proj-…`) was committed early in
this repo's history. It is still reachable from old commits.

**Revoke it** in the OpenAI dashboard → *API keys*, and issue a replacement that
lives only in a git-ignored `.env`. Revocation is the step that actually contains
the leak — the purge below is hygiene, and cannot un-share a key that was already
readable. See [SECURITY.md](../SECURITY.md).

> Do not make the repository public before this is done. Public repositories are
> scraped by credential bots within seconds.

## 2. Purge the secret from history

Rewrites every commit SHA, so everyone must re-clone afterwards. Decide first
whether to keep or delete the stale branches — they get rewritten too.

```bash
pip install git-filter-repo
git clone --mirror https://github.com/Aaditya2605/checkpoint.git ckpt-mirror
cd ckpt-mirror
git filter-repo --invert-paths --path .env --force
git push --force --all && git push --force --tags
```

Old blobs can remain reachable by direct SHA URL; GitHub Support can purge the
cached views.

Verify:

```bash
git rev-list --all --objects | grep -E '\s\.env$'   # expect no output
```

## 3. Make the repository public

Settings → General → Danger Zone → *Change visibility*. **Requires repo admin.**

## 4. Turn on the free public-repo protections

Settings → Code security:

- **Secret scanning** — catches a committed credential after the fact.
- **Push protection** — blocks the commit that would leak one. This is what
  prevents a repeat of step 1.

## 5. Make the security gates blocking

Both gates are currently non-blocking for reasons that disappear once the repo is
public and the history is purged. Delete one line from each:

| File | Line to delete | Why it was there |
|---|---|---|
| `.github/workflows/codeql.yml` | `continue-on-error: true` | Code scanning needs Advanced Security on a private repo; it is free once public. Verify with `gh api repos/<owner>/<repo>/code-scanning/alerts` — a 403 means it is still off. |
| `.github/workflows/gitleaks.yml` | `continue-on-error: true` | The full-history scan flagged the key from step 1. Safe to enforce after the purge. |

Blocking coverage does not depend on these: `tests/test_no_tracked_secrets.py`
runs in the gating test job and fails on a real provider key in any tracked file.

## 6. Publish to PyPI

The release pipeline (`.github/workflows/release.yml`) uses **trusted publishing**
— no API token is stored. One-time setup on PyPI:

1. Create/own the `checkpoint-agents` project on https://pypi.org.
2. Project → *Publishing* → add a trusted publisher:
   owner `Aaditya2605`, repo `checkpoint`, workflow `release.yml`,
   environment `pypi`. (Use a *pending publisher* for the very first release.)

Then tag:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow builds the dashboard, runs the full test suite on the tagged commit,
builds the sdist + wheel, verifies the wheel ships every runtime asset (SPA, demo
assets, sidecar Dockerfile, init templates, twin seed fixtures), and publishes.

## 7. Switch the documented install to PyPI

Until the release lands, the README installs from git on purpose. Afterwards:

- `README.md` — replace both `pip install git+https://github.com/Aaditya2605/checkpoint`
  lines with `pip install checkpoint-agents`, and drop the "Not on PyPI yet"
  paragraph.
- The GitHub Action already prefers the published distribution and falls back to
  a source install, so it needs no change.

`tests/test_install_instructions.py` fails the build if any tracked file
recommends the bare `checkpoint` distribution (an unrelated existing project).

## 8. Polish the public face

- Repository description and topics (admin).
- Enable Discussions — the issue-template chooser links to it.
- Branch protection on `main`: require the `Build SPA + run pytest + verify wheel`
  and `Validate the GitHub Action` checks.
