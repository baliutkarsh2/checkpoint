#!/usr/bin/env python3
"""Checkpoint harness — Anthropic SDK with MCP tools.

The twin's MCP server is mounted at {CHECKPOINT_<CLONE>_URL}/mcp/ and
speaks the standard streamable-HTTP MCP protocol. Claude reads tools
from it automatically when you pass the mcp_servers list.

Requirements:
    pip install anthropic>=0.40

Env vars set by Checkpoint:
    CHECKPOINT_TASK          - the scenario prompt
    CHECKPOINT_GITHUB_URL    - twin base URL (e.g. http://127.0.0.1:8001)
    GITHUB_TOKEN             - bootstrap auth token

Contract:
    Print exactly one JSON line to stdout: {"text": "<answer>"}
    Exit 0 on success.
"""
from __future__ import annotations

import json
import os
import sys

TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""
CLONE_URL = os.environ.get("CHECKPOINT_GITHUB_URL", "")
CLONE_TOKEN = os.environ.get("GITHUB_TOKEN", "")

try:
    import anthropic
except ImportError:
    print(json.dumps({"text": "anthropic package not installed — run: pip install anthropic"}))
    sys.exit(1)


def main() -> None:
    print(f"[harness] task: {TASK[:200]}", file=sys.stderr)

    client = anthropic.Anthropic()

    mcp_server_url = f"{CLONE_URL}/mcp/" if CLONE_URL else None

    messages: list[dict] = [{"role": "user", "content": TASK}]

    # Build extra_headers so the twin accepts the token.
    # In real Anthropic MCP usage the SDK handles OAuth; here we pass the
    # bootstrap token directly as a Bearer credential on the MCP sub-request.
    kwargs: dict = {
        "model": "claude-opus-4-7",
        "max_tokens": 4096,
        "messages": messages,
    }

    if mcp_server_url:
        kwargs["mcp_servers"] = [
            {
                "type": "url",
                "url": mcp_server_url,
                "name": "checkpoint-twin",
                "authorization_token": CLONE_TOKEN,
            }
        ]

    response = client.beta.messages.create(
        **kwargs,
        betas=["mcp-client-2025-04-04"],
    )

    # Extract the last text block as the final answer.
    answer = ""
    for block in reversed(response.content):
        if hasattr(block, "text"):
            answer = block.text
            break

    print(json.dumps({"text": answer}))


if __name__ == "__main__":
    main()
