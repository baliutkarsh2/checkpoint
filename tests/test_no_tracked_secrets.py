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
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),   # OpenAI project keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),     # Anthropic keys
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),         # Google API keys
]

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
        # Skip binary-ish assets and the committed SPA bundle.
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
        for pat in _REAL_KEY_PATTERNS:
            if pat.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)} matched {pat.pattern}")
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
