# Security Policy

## Reporting a vulnerability

Email **security@usecheckpoint.dev** (or hello@usecheckpoint.dev) with details and a
proof-of-concept if you have one. Please do not open a public issue for security
reports. We aim to acknowledge within 48 hours.

Checkpoint is a testing tool that intercepts TLS and runs agent code; if you find a
way for a scenario, harness, or dashboard request to escape its intended boundary,
that is in scope and we want to hear about it.

---

## ⚠️ Action required: rotate the previously-committed OpenAI key

**This repository's history is clean.** It was created from a predecessor repo
whose history was rewritten with `git filter-repo --invert-paths --path .env`
before publishing, so no `.env` blob and no key material exists in any commit
here — verified: `git log --all -- .env` returns nothing.

**The key itself must still be rotated.** It was readable in the predecessor
repo's history, so treat it as compromised regardless of the rewrite: a purge
cannot un-share a secret that was already published.

### 1. Rotate the key (the only step that actually contains the leak)
- Revoke the key in the OpenAI dashboard → *API keys*.
- Issue a replacement and put it only in a local, git-ignored `.env` (see
  `.env.example`).

### 2. Turn on the guardrails so it can't recur
- Enable **Secret scanning** + **Push protection** in the repo's
  *Settings → Code security and analysis*.
- The CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push/PR (see
  `.github/workflows/`), and `tests/test_no_tracked_secrets.py` fails the normal test
  suite if a real-shaped key or an unmarked synthetic token is ever committed.

---

## Synthetic credentials

The local twins use synthetic bootstrap tokens (GitHub/Stripe/Slack/… style). These
are **never real**: each keeps its SDK-required prefix so client libraries accept it,
but its body carries the literal marker `CHECKPOINTFAKE`. They live in one place —
`checkpoint/fake_credentials.py` — and are allow-listed in `.gitleaks.toml`. Do not
add new hardcoded token strings elsewhere; import the constant instead.

## Running the dashboard safely

`checkpoint serve` binds to loopback (`127.0.0.1`) by default and needs no auth there.
If you bind it to any other interface, you **must** set `CHECKPOINT_DASHBOARD_API_KEY`
(the server refuses to start on a non-loopback bind without it). `POST /api/jobs`
executes agent harnesses; only run the dashboard against code you trust, and use
`CHECKPOINT_DASHBOARD_READ_ONLY=1` for viewer-only instances.
