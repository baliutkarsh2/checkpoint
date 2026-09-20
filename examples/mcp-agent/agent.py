#!/usr/bin/env python3
"""A GitHub agent whose tools come from an MCP server instead of from this file.

Nothing here declares a tool. The client connects, asks the server what it can
do, and hands that list to the model — so the agent's capabilities change when
the server changes, not when this file does.

Checkpoint's GitHub twin speaks MCP as well as REST and exposes the same
operations, so the only thing that differs between production and a test run is
the server address.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from mcp import ClientSession
from openai import OpenAI

# mcp 2.0 renamed `streamablehttp_client` to `streamable_http_client` and
# changed what it yields from (read, write, get_session_id) to (read, write).
# An agent that pins itself to one major stops working on the other, so this
# accepts both — which is what the `mcp>=1.9` in requirements.txt means.
try:
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client


@asynccontextmanager
async def open_streams(url: str):
    """(read, write) from either mcp major."""
    async with _http_client(url) as streams:
        yield streams[0], streams[1]

MODEL = os.environ.get("AGENT_MODEL", "gpt-5.6-luna")
MAX_STEPS = 10

# An MCP client is told where its server is; it never guesses. In production
# that is the hosted GitHub MCP endpoint, and under Checkpoint it is the twin,
# whose base URL the sandbox puts in the environment.
MCP_URL = os.environ.get("GITHUB_MCP_URL") or (
    os.environ.get("CHECKPOINT_GITHUB_URL", "https://api.githubcopilot.com").rstrip("/") + "/mcp/"
)

SYSTEM = """You maintain the acme/webapp repository.
Work only through the tools you were given, one step at a time, and check what a
tool returned before deciding the next one. Finish with one short paragraph
naming every issue you touched and what you did to it."""


def as_openai_tool(tool) -> dict:
    """An MCP tool declaration in the shape the chat-completions API wants."""
    return {"type": "function", "function": {
        "name": tool.name,
        "description": tool.description or "",
        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
    }}


def as_text(result) -> str:
    """Flatten an MCP tool result into something a model can read."""
    parts = [getattr(block, "text", "") for block in (result.content or [])]
    return "\n".join(part for part in parts if part) or "(no output)"


async def run(task: str) -> str:
    llm = OpenAI()
    async with open_streams(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [as_openai_tool(t) for t in (await session.list_tools()).tools]

            messages: list = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": task},
            ]
            for _ in range(MAX_STEPS):
                reply = llm.chat.completions.create(model=MODEL, messages=messages, tools=tools)
                message = reply.choices[0].message
                messages.append(message)
                if not message.tool_calls:
                    return message.content or ""
                for call in message.tool_calls:
                    arguments = json.loads(call.function.arguments or "{}")
                    try:
                        content = as_text(await session.call_tool(call.function.name, arguments))
                    except Exception as e:
                        # A refusal from the server is information the model can
                        # act on, so it goes back into the conversation.
                        content = f"error: {e}"
                    messages.append({
                        "role": "tool", "tool_call_id": call.id, "content": content,
                    })
            return "Ran out of steps before finishing."


if __name__ == "__main__":
    print(asyncio.run(run(os.environ["CHECKPOINT_TASK"])))
