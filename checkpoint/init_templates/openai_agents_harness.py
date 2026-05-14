#!/usr/bin/env python3
"""Checkpoint harness — OpenAI Agents SDK with MCP tools.

The twin's MCP server is at {CHECKPOINT_<CLONE>_URL}/mcp/. The Agents SDK
connects to it via MCPServerStreamableHttp and exposes all tools to the agent
automatically.

Requirements:
    pip install openai-agents>=0.0.3

Env vars set by Checkpoint:
    CHECKPOINT_TASK          - the scenario prompt
    CHECKPOINT_GITHUB_URL    - twin base URL
    GITHUB_TOKEN             - bootstrap auth token

Contract:
    Print exactly one JSON line to stdout: {"text": "<answer>"}
    Exit 0 on success.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""
CLONE_URL = os.environ.get("CHECKPOINT_GITHUB_URL", "")
CLONE_TOKEN = os.environ.get("GITHUB_TOKEN", "")

try:
    from agents import Agent, Runner
    from agents.mcp import MCPServerStreamableHttp
except ImportError:
    print(json.dumps({"text": "openai-agents package not installed — run: pip install openai-agents"}))
    sys.exit(1)


async def run_agent() -> str:
    mcp_servers = []
    if CLONE_URL:
        server = MCPServerStreamableHttp(
            url=f"{CLONE_URL}/mcp/",
            headers={"Authorization": f"Bearer {CLONE_TOKEN}"} if CLONE_TOKEN else {},
        )
        mcp_servers.append(server)

    agent = Agent(
        name="checkpoint-agent",
        model="gpt-4o",
        mcp_servers=mcp_servers,
    )

    result = await Runner.run(agent, TASK)
    return str(result.final_output or "")


def main() -> None:
    print(f"[harness] task: {TASK[:200]}", file=sys.stderr)
    answer = asyncio.run(run_agent())
    print(json.dumps({"text": answer}))


if __name__ == "__main__":
    main()
