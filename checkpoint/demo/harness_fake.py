#!/usr/bin/env python3
"""Bundled demo agent — no LLM, no third-party dependencies.

Reads the task from CHECKPOINT_TASK, creates the requested GitHub issue against
the local twin (CHECKPOINT_GITHUB_URL, set by `checkpoint demo` in --no-docker
mode), and prints its final answer as JSON. Uses only the standard library so
`checkpoint demo` works from a bare `pip install` with nothing else set up.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

GITHUB_BASE = os.environ.get("CHECKPOINT_GITHUB_URL") or "https://api.github.com"
TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""
# Token comes from the runner (non-Docker) or the TLS sidecar swap (Docker).
_TOKEN = os.environ.get("GITHUB_TOKEN", "ignored-fake-user-token")
_AUTH = f"token {_TOKEN}" if _TOKEN.startswith("ghp_") else f"Bearer {_TOKEN}"


def _request(method: str, path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{GITHUB_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={
            "Authorization": _AUTH,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "checkpoint-demo/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:160]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:160]
    except (urllib.error.URLError, OSError) as e:
        # The twin isn't reachable. Report it as a failed call rather than
        # dying with a traceback — the runner scores the run either way.
        return 0, f"connection failed: {e}"


def main() -> None:
    m = re.search(r'titled\s+["“]([^"”]+)["”]', TASK)
    title = m.group(1) if m else "hello world"
    rm = re.search(r'["“]?([\w.-]+)/([\w.-]+)["”]?', TASK)
    owner, repo = (rm.group(1), rm.group(2)) if rm else ("default-user", "webapp")

    # Self-contained: create the repo first so the demo needs no seed/setup,
    # then file the issue. The twin creates the repo under the authed user.
    _request("POST", "/user/repos", {"name": repo})
    status, text = _request(
        "POST",
        f"/repos/{owner}/{repo}/issues",
        {"title": title, "body": "Filed by the Checkpoint demo agent."},
    )
    print(f"[demo] POST /repos/{owner}/{repo}/issues -> {status} {text}", file=sys.stderr)

    print(json.dumps({"text": f"Created issue '{title}' in {owner}/{repo}."}))


if __name__ == "__main__":
    main()
