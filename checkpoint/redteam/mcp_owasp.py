"""OWASP MCP Top 10 — the attack surface a compromised MCP server presents.

Coding agents auto-connect to MCP servers and call their tools at developer
privilege. A malicious or compromised server can hide instructions in a tool's
description (so the model follows them), expose a destructive "shadow" tool, or
inject content into a tool result. `mcp_poison` builds a deliberately hostile MCP
server you can point your agent at, to check it doesn't get steered.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McpCategory:
    id: str
    name: str
    description: str


OWASP_MCP: dict[str, McpCategory] = {
    c.id: c for c in [
        McpCategory("MCP01", "Token Mismanagement", "Credentials mishandled or over-shared by the server."),
        McpCategory("MCP02", "Privilege Scope Creep", "The server requests or exercises more scope than needed."),
        McpCategory("MCP03", "Tool Poisoning", "A tool's description hides instructions the model then follows."),
        McpCategory("MCP04", "Supply-Chain Compromise", "A dependency or upstream server is malicious."),
        McpCategory("MCP05", "Command Injection", "Tool inputs flow into shell/command execution."),
        McpCategory("MCP06", "Intent-Flow Subversion", "The server redirects the agent's goal mid-task."),
        McpCategory("MCP07", "Weak Authn/Authz", "Missing or weak authentication on the server."),
        McpCategory("MCP08", "Missing Audit Telemetry", "The server hides what tools were actually invoked."),
        McpCategory("MCP09", "Shadow / Rogue Server", "An unexpected server exposes destructive tools."),
        McpCategory("MCP10", "Context Injection", "Instructions are injected via tool result content."),
    ]
}


def describe(mcp_id: str) -> McpCategory | None:
    return OWASP_MCP.get((mcp_id or "").strip().upper())
