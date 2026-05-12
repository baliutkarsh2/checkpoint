#!/usr/bin/env python3
"""Smoke-test harness: doesn't use an LLM. Executes the task directly against
`https://api.github.com` via stock `requests`. The TLS sidecar intercepts and
forwards to the local twin — this is the deterministic acceptance gate for
Phase 1's route-mode interception.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

GITHUB_BASE = "https://api.github.com"
TASK = os.environ.get("ARCHAL_ENGINE_TASK") or os.environ.get("CHECKPOINT_TASK") or ""
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/archal-out"))

HEADERS = {
    "Authorization": "Bearer ignored-fake-user-token",
    "Accept": "application/vnd.github+json",
    "User-Agent": "checkpoint-fake-harness/0.1",
}


def main():
    m = re.search(r'titled\s+["“]([^"”]+)["”]', TASK)
    title = m.group(1) if m else "untitled"

    repo_match = re.search(r'["“]?([\w.-]+)/([\w.-]+)["”]', TASK)
    owner, repo = (repo_match.group(1), repo_match.group(2)) if repo_match else ("acme", "webapp")

    # Deterministic friendly body — keeps the scoring stable across runs.
    body = "Hello, friend! Hope you are having a wonderful day. Cheers!"

    url = f"{GITHUB_BASE}/repos/{owner}/{repo}/issues"
    print(f"[fake] POST {url} title={title!r}", file=sys.stderr)
    r = requests.post(
        url,
        json={"title": title, "body": body},
        headers=HEADERS,
        timeout=15,
    )
    print(f"[fake] -> {r.status_code} {r.text[:160]}", file=sys.stderr)

    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "metrics.json").write_text(json.dumps({
            "version": 1, "llmCallCount": 0, "toolCallCount": 1,
            "toolErrorCount": 0 if r.ok else 1, "exitReason": "completed",
            "provider": "none", "model": "fake",
        }))
        (ARCHAL_OUT / "agent-trace.json").write_text(json.dumps({
            "version": 1,
            "final": f"Created issue '{title}' in {owner}/{repo}.",
            "events": [{"method": "POST", "path": f"/repos/{owner}/{repo}/issues",
                        "status": r.status_code}],
        }))
    except OSError as e:
        print(f"[archal-out write failed: {e}]", file=sys.stderr)

    print(json.dumps({"text": f"Created issue '{title}' in {owner}/{repo}."}))


if __name__ == "__main__":
    main()
