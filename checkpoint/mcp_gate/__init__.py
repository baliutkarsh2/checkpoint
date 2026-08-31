"""Checkpoint exposed AS an MCP server.

So a coding agent (Claude Code, Cursor, any MCP client) can test the very agent
it is building, inline, during development — list scenarios, run one, or gate a
whole directory, as MCP tool calls. The tool *logic* lives in plain functions in
`tools.py` (unit-tested directly); `server.py` wraps them as MCP tools.
"""
from .tools import gate_tool, list_scenarios_tool, run_scenario_tool

__all__ = ["gate_tool", "list_scenarios_tool", "run_scenario_tool"]
