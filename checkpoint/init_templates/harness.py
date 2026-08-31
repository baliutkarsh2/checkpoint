#!/usr/bin/env python3
"""Checkpoint harness — replace this body with your agent.

Contract:
  - Read CHECKPOINT_TASK from the environment (the scenario's `## Prompt`).
  - Read CHECKPOINT_<CLONE>_URL for each clone in scope (e.g. CHECKPOINT_GITHUB_URL).
  - Do the work (real LLM, hardcoded steps, whatever your agent is).
  - Print ONE JSON object to stdout: {"text": "your final answer"}.
  - Exit 0.

This template uses raw `requests` so you can see the wire format. Swap in
your framework of choice (LangChain, the OpenAI Agents SDK, Anthropic
tools, etc.) — Checkpoint doesn't care what's inside.
"""
from __future__ import annotations

import json
import os
import sys

import requests
from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN

TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""
GITHUB_URL = os.environ.get("CHECKPOINT_GITHUB_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)


def example_github_call() -> dict:
    """Tiny example: list repos. Replace with your real agent logic."""
    if not GITHUB_URL:
        return {"skipped": "no CHECKPOINT_GITHUB_URL set"}
    r = requests.get(
        f"{GITHUB_URL}/user/repos",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    return {"status": r.status_code, "count": len(r.json()) if r.ok else 0}


def main() -> None:
    print(f"[harness] task: {TASK[:200]}", file=sys.stderr)

    # --- Replace this block with your agent ---
    result = example_github_call()
    answer = f"Stub harness executed. Inspected {result.get('count', 0)} repos."
    # --- End replace ---

    print(json.dumps({"text": answer}))


if __name__ == "__main__":
    main()
