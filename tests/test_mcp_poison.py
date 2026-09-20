"""The poisoned MCP server must really be poisoned, whichever technique is asked for.

An agent that trusts tool metadata can be driven by whoever wrote it. These
tests pin that the bundled attack server hides an instruction in a benign tool's
description, exposes the destructive tool that instruction points at, records
when the trap is taken, and never quietly degrades into a harmless server.
"""
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


def test_serve_poisoned_serves_the_technique_it_was_asked_for(monkeypatch):
    """The flag has to reach the server: an un-poisoned server tests nothing."""
    import checkpoint.redteam as redteam_pkg

    seen: dict = {}

    class _Server:
        def run(self, transport):
            seen["transport"] = transport

    def _build(**kwargs):
        seen.update(kwargs)
        return _Server(), {}

    monkeypatch.setattr(redteam_pkg, "build_poisoned_server", _build)
    r = CliRunner().invoke(main, ["redteam", "serve-poisoned", "--technique", "MCP06"])
    assert r.exit_code == 0, r.output
    assert seen["technique"] == "MCP06"
    # stdout is the MCP transport, so the command must not write to it.
    assert seen["transport"] == "stdio"
    assert r.output == ""


def test_serve_poisoned_refuses_a_technique_it_does_not_implement():
    r = CliRunner().invoke(main, ["redteam", "serve-poisoned", "--technique", "MCP99"])
    assert r.exit_code != 0
    assert "MCP99" in r.output
