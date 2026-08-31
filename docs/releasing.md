# Releasing and going public

The runbook for taking Checkpoint from a private repo to a published, public
project. Steps are ordered; several need repository-admin or account access.

---

## 1. Rotate the previously-leaked key — **do this first**

This repository's history is already clean (see step 2), but a live-format
OpenAI key was readable in the predecessor repo's history, so it must be treated
as compromised. **Revoke it** in the OpenAI dashboard → *API keys* and issue a
replacement that lives only in a git-ignored `.env`. A history rewrite cannot
un-share a secret that was already published. See [SECURITY.md](../SECURITY.md).

## 2. Confirm the history is secret-free — **done, but verify before publishing**

This repo was created from a predecessor whose history was rewritten with
`git filter-repo --invert-paths --path .env`, so no key material exists in any
commit here. Re-verify before flipping visibility:

```bash
git log --all --oneline -- .env                  # expect no output
git rev-list --all --objects | grep -E '\s\.env$'  # expect no output
git log --all --oneline -S'sk-proj-'             # only docs + test regex patterns
```

The last command legitimately matches `SECURITY.md`, this file, and
`tests/test_no_tracked_secrets.py`, which contain the *pattern* `sk-proj-…`
rather than a key.

## 3. Make the repository public

Settings → General → Danger Zone → *Change visibility*. **Requires repo admin.**

## 4. Turn on the free public-repo protections

Settings → Code security:

- **Secret scanning** — catches a committed credential after the fact.
- **Push protection** — blocks the commit that would leak one. This is what
  prevents a repeat of step 1.

## 5. Security gates are blocking — done

Both gates now fail the build rather than reporting and continuing: gitleaks
(this history is clean) and CodeQL (code scanning is free and enabled on a public
repo). Verify code scanning with `gh api repos/<owner>/<repo>/code-scanning/alerts`
— a 403 means it is off; "no analysis found" just means none uploaded yet.


Blocking coverage does not depend on these: `tests/test_no_tracked_secrets.py`
runs in the gating test job and fails on a real provider key in any tracked file.

## 6. Publish to PyPI

The release pipeline (`.github/workflows/release.yml`) uses **trusted publishing**
— no API token is stored. One-time setup on PyPI:

1. Create/own the `checkpoint-agents` project on https://pypi.org.
2. Project → *Publishing* → add a trusted publisher:
   owner `baliutkarsh2`, repo `checkpoint`, workflow `release.yml`,
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

- `README.md` — replace both `pip install git+https://github.com/baliutkarsh2/checkpoint`
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
