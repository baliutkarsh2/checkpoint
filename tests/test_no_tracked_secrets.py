"""Guard against committing real credentials.

This is a cheap tripwire that runs in the normal pytest suite (no network, no
gitleaks binary required). It complements the gitleaks CI job:

  1. `.env` (and friends) must never be tracked by git.
  2. No real-shaped LLM provider key may appear in any tracked file.
  3. Every synthetic twin token (sk_live_… / ghp_… / xoxb-… style) that we DO
     commit must carry the CHECKPOINTFAKE marker, proving it is not real.

If a real key ever lands in the tree, (2) fails loudly here long before it
reaches a reviewer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout; secret tripwire skipped")
    return [line for line in out.stdout.splitlines() if line.strip()]


# Real-shaped provider secrets. These patterns intentionally do NOT match the
# synthetic twin tokens, which use an underscore prefix (sk_live_) or the
# CHECKPOINTFAKE marker.
_REAL_KEY_PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),     # OpenAI project keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),      # Anthropic keys
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),         # Google API keys
    # OpenAI legacy/user keys (sk- followed by a long base62 run). The hyphen
    # forms above are matched separately; `sk_live_` synthetic tokens use an
    # underscore and are deliberately not matched here.
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),            # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),            # AWS temporary access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),    # GitHub PAT / OAuth / refresh
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}"),  # GitHub fine-grained PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),  # Slack tokens
    re.compile(r"\bsk_live_[A-Za-z0-9]{20,}"),      # Stripe live secret key
    re.compile(r"\brk_live_[A-Za-z0-9]{20,}"),      # Stripe live restricted key
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),     # GitLab PAT
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),           # Hugging Face token
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
]

# The marker every deliberately-synthetic token must carry.
_FAKE_MARKER = "CHECKPOINTFAKE"

# Synthetic-token prefixes we deliberately ship. Every occurrence must be fake.
_SYNTHETIC_PREFIXES = re.compile(r"(sk_live_[A-Za-z0-9]+|ghp_[A-Za-z0-9]+|xoxb-[A-Za-z0-9-]+)")

# This test file itself contains the example patterns above; never scan it.
_SELF = Path(__file__).name


def _text_files() -> list[Path]:
    files: list[Path] = []
    for rel in _tracked_files():
        if rel == f"tests/{_SELF}":
            continue
        path = REPO_ROOT / rel
        # Skip the built SPA bundle if a local build left one in the tree
        # (it is gitignored, so normally git ls-files won't surface it).
        if rel.startswith("checkpoint/dashboard/static/"):
            continue
        if path.suffix in {".png", ".ico", ".woff", ".woff2", ".ttf", ".jpg", ".jpeg", ".gif"}:
            continue
        files.append(path)
    return files


def test_env_file_not_tracked():
    tracked = _tracked_files()
    offenders = [f for f in tracked if f == ".env" or (f.startswith(".env.") and f != ".env.example")]
    assert not offenders, f"secret env file(s) tracked by git: {offenders}"


def test_no_real_provider_keys_in_tree():
    offenders: list[str] = []
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            # The twins ship deliberately synthetic tokens that are shaped like
            # the real thing on purpose. Every one carries the CHECKPOINTFAKE
            # marker (enforced by test_synthetic_tokens_are_marked_fake), so a
            # marked line is exempt — anything else matching is a real leak.
            if _FAKE_MARKER in line:
                continue
            for pat in _REAL_KEY_PATTERNS:
                if pat.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_no} matched {pat.pattern}"
                    )
    assert not offenders, "real-shaped provider key(s) found in tracked files:\n" + "\n".join(offenders)


def test_synthetic_tokens_are_marked_fake():
    """Any committed sk_live_/ghp_/xoxb- token must be an obvious CHECKPOINTFAKE."""
    offenders: list[str] = []
    for path in _text_files():
        # The gitleaks config documents the allowlist; skip it.
        if path.name == ".gitleaks.toml":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _SYNTHETIC_PREFIXES.finditer(text):
            if "CHECKPOINTFAKE" not in m.group(0):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {m.group(0)[:40]}")
    assert not offenders, (
        "synthetic token(s) without the CHECKPOINTFAKE marker "
        "(centralize in checkpoint/fake_credentials.py):\n" + "\n".join(offenders)
    )
