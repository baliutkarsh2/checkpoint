#!/usr/bin/env python3
"""MCP-client agent — talks to the twin's /mcp/ surface, not its REST API.

Demonstrates that Checkpoint twins are valid MCP servers. An agent that
discovers and calls tools via the Model Context Protocol works against
Checkpoint exactly the same way it works against the real-vendor MCP servers
(github-mcp-server, linear-mcp, slack-mcp, etc.).

This harness uses Anthropic's tool-use API for the LLM brain and the official
`mcp` Python SDK to talk to the twin's MCP server.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("CHECKPOINT_AGENT_MODEL", "claude-sonnet-4-6")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "12"))

# In docker mode we DNS-hijack production URLs to the sidecar; the MCP
# endpoint sits at the same host. For the stock agent we point at the GitHub
# twin's MCP — same shape works for any twin.
MCP_URL = os.environ.get(
    "CHECKPOINT_MCP_URL",
    "https://api.github.com/mcp/",
)

LLM_CALLS = 0
TOOL_CALLS = 0


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _to_anthropic_tool(t) -> dict:
    """Translate an MCP Tool into an Anthropic tool spec."""
    return {
        "name": t.name,
        "description": t.description or "",
        "input_schema": t.inputSchema or {"type": "object", "properties": {}},
    }


MESSAGES_FOR_TRACE: list[dict] = []
TOOL_CALLS_LOG: list[dict] = []
USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    out: list[str] = []
    for b in content:
        bt = getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
        if bt == "text":
            out.append(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else ""))
        elif bt == "tool_use":
            n = getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None)
            i = getattr(b, "input", None) or (b.get("input") if isinstance(b, dict) else None)
            out.append(f"[tool_use {n}({json.dumps(i, default=str)})]")
        elif bt == "tool_result":
            c = getattr(b, "content", None) or (b.get("content") if isinstance(b, dict) else None)
            out.append(f"[tool_result {c}]")
    return "\n".join(p for p in out if p)


async def run_agent():
    global LLM_CALLS, TOOL_CALLS
    client = Anthropic()

    async with streamablehttp_client(MCP_URL) as (read, write, _meta):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tools = [_to_anthropic_tool(t) for t in tools_resp.tools]
            _log(f"[mcp] discovered {len(tools)} tools from {MCP_URL}")

            messages: list[dict] = [{"role": "user", "content": TASK}]
            final_text = ""
            for step in range(MAX_STEPS):
                LLM_CALLS += 1
                _log(f"[agent] step {step + 1}/{MAX_STEPS}")
                resp = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    tools=tools,
                    messages=messages,
                    system=(
                        "You are an autonomous agent that uses MCP tools to "
                        "complete the user's task. Be precise. End with a "
                        "final text response naming every entity you touched."
                    ),
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    inp = getattr(u, "input_tokens", 0) or 0
                    outp = getattr(u, "output_tokens", 0) or 0
                    USAGE["prompt_tokens"] += inp
                    USAGE["completion_tokens"] += outp
                    USAGE["total_tokens"] += inp + outp
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    final_text = "".join(
                        getattr(b, "text", "") for b in resp.content
                        if getattr(b, "type", "") == "text"
                    )
                    break

                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    TOOL_CALLS += 1
                    args = block.input or {}
                    try:
                        result = await session.call_tool(block.name, args)
                        payload = [
                            {"type": getattr(c, "type", "text"),
                             "text": getattr(c, "text", str(c))}
                            for c in (result.content or [])
                        ]
                        status = "ok"
                    except Exception as e:
                        payload = [{"type": "text", "text": f"error: {e}"}]
                        status = "error"
                    TOOL_CALLS_LOG.append({
                        "name": block.name, "input": args, "output": payload, "status": status,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload),
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                final_text = final_text or "Reached max steps."
            # Flatten messages for the dashboard's chat view.
            MESSAGES_FOR_TRACE.extend(
                {"role": m.get("role", ""), "content": _content_to_text(m.get("content"))}
                for m in messages
            )
            return final_text


def main():
    final_text = asyncio.run(run_agent())
    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({
                "version": 1, "llmCallCount": LLM_CALLS,
                "toolCallCount": TOOL_CALLS, "toolErrorCount": 0,
                "exitReason": "completed", "provider": "anthropic+mcp", "model": MODEL,
                "promptTokens": USAGE["prompt_tokens"],
                "completionTokens": USAGE["completion_tokens"],
                "totalTokens": USAGE["total_tokens"],
            }, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({
                "version": 2, "final": final_text, "events": [],
                "messages": MESSAGES_FOR_TRACE,
                "tool_calls": TOOL_CALLS_LOG,
                "usage": USAGE,
                "provider": "anthropic+mcp", "model": MODEL,
            }, f, default=str)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")
    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
