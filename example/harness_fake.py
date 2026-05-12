#!/usr/bin/env python3
"""Smoke-test harness: doesn't use an LLM. Just executes the task directly.

For 'Create an issue titled "hello world"' it posts the issue and exits. Used
to verify checkpoint's wiring (twin lifecycle, env vars, trace fetch, [D]
evaluation) without burning OpenAI calls.
"""
from __future__ import annotations

import json
import os
import re
import sys

import httpx

BASE_URL = os.environ.get("CHECKPOINT_GITHUB_URL") or os.environ.get("CHECKPOINT_BASE_URL")
TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""


def main():
    if not BASE_URL:
        print("Missing CHECKPOINT_GITHUB_URL", file=sys.stderr)
        sys.exit(1)

    m = re.search(r'titled\s+["“]([^"”]+)["”]', TASK)
    title = m.group(1) if m else "untitled"

    repo_match = re.search(r'(?:in\s+(?:repo(?:sitory)?\s+)?)?["“]?([\w.-]+)/([\w.-]+)["”]?', TASK)
    owner, repo = (repo_match.group(1), repo_match.group(2)) if repo_match else ("acme", "webapp")

    body_match = re.search(r"(?:with a |body[^\w]*)([^.]{0,200})", TASK)
    body = (body_match.group(1) if body_match else "hello, friend!").strip()

    print(f"[fake] POST {owner}/{repo} title={title!r}", file=sys.stderr)
    r = httpx.post(
        f"{BASE_URL}/repos/{owner}/{repo}/issues",
        json={"title": title, "body": body},
        timeout=10,
    )
    print(f"[fake] -> {r.status_code} {r.text[:120]}", file=sys.stderr)

    print(json.dumps({"text": f"Created issue '{title}' in {owner}/{repo}."}))


if __name__ == "__main__":
    main()
