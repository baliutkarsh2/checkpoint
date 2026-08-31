"""Poisoned MCP server (OWASP MCP Top 10) attack surface."""
from __future__ import annotations

import asyncio

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.redteam import build_poisoned_server, poison_description
from checkpoint.redteam.mcp_owasp import OWASP_MCP, describe


def test_owasp_mcp_catalog_complete():
    assert set(OWASP_MCP) == {f"MCP{n:02d}" for n in range(1, 11)}
    assert describe("mcp03").name == "Tool Poisoning"
    assert describe("nope") is None


def test_poison_description_injects_per_technique():
    clean = "Search the knowledge base."
    assert "<IMPORTANT>" in poison_description(clean, "MCP03")
    assert "real intent" in poison_description(clean, "MCP06")
    assert "SYSTEM OVERRIDE" in poison_description(clean, "MCP10")
    assert clean in poison_description(clean, "MCP03")
    # Unknown technique falls back to a real injection, never a no-op.
    assert poison_description(clean, "??") != clean


def test_poisoned_server_has_injected_tool_and_trap():
    server, state = build_poisoned_server(technique="MCP03")
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert "search" in by_name and "admin_delete_all" in by_name
    # The benign tool's description carries the hidden instruction.
    assert "<IMPORTANT>" in (by_name["search"].description or "")
    assert state["trap_called"] is False


def test_poisoned_server_trap_records_invocation():
    server, state = build_poisoned_server()
    # Call the trap tool as an agent would, and confirm it's recorded.
    result = asyncio.run(server.call_tool("admin_delete_all", {}))
    assert state["trap_called"] is True
    assert result is not None


def test_redteam_mcp_command_registered():
    r = CliRunner().invoke(main, ["redteam-mcp", "--help"])
    assert r.exit_code == 0
    assert "POISONED" in r.output
