"""Checkpoint as an MCP server, so a coding agent can test while it writes.

Registered with an MCP client as command ``checkpoint``, args ``["mcp"]``, this
gives Claude Code, Cursor or any other client the same four things a developer
does at the terminal: what scenarios exist, how each criterion will be decided,
what one run did, and whether the build ships.

The tool descriptions are the only instructions the model gets, so they say what
a result *means* — INCONCLUSIVE is not a failure, an unscored run is not a
verdict — rather than only what the arguments are.
"""
from __future__ import annotations

from checkpoint.mcp_compat import FastMCP, make_server

from .tools import check_scenario_tool, gate_tool, list_scenarios_tool, run_scenario_tool


def build_server() -> FastMCP:
    mcp = make_server("checkpoint")

    @mcp.tool()
    def list_scenarios(scenarios_dir: str | None = None) -> list[dict]:
        """List this project's Checkpoint scenarios: each one's task, the
        services (twins) it runs against, and its success criteria. Defaults to
        the scenario directory checkpoint.toml points at."""
        return list_scenarios_tool(scenarios_dir)

    @mcp.tool()
    def check_scenario(scenario_path: str) -> dict:
        """Show how each criterion in a scenario will be decided, without running
        anything. Use this after writing or editing criteria: it returns the
        assertion behind each one, which is how you catch a criterion that an
        agent doing nothing would already pass."""
        return check_scenario_tool(scenario_path)

    @mcp.tool()
    def run_scenario(scenario_path: str, command: str | None = None,
                     judge_model: str | None = None) -> dict:
        """Run one scenario once against the agent and report what it actually
        did: the score, each criterion's verdict and why, the number of API calls,
        and anything that could not be scored. `command` defaults to the agent
        declared in checkpoint.toml. One run is a debugging signal, not evidence
        for a release — use `gate` for that."""
        return run_scenario_tool(scenario_path, command, judge_model)

    @mcp.tool()
    def gate(target: str | None = None, command: str | None = None, runs: int = 16,
             pass_threshold: float = 80.0) -> dict:
        """Decide whether an agent change is safe to ship: run every scenario
        `runs` times and return SHIP / CONDITIONAL / INCONCLUSIVE / BLOCK / ERROR
        with per-scenario pass rates and confidence intervals.

        Only SHIP means the evidence supports a release. INCONCLUSIVE means
        `runs` was too small to decide and the reply says how many would be
        enough — fewer than 16 can never reach SHIP at the default threshold.
        ERROR means the sandbox, judge or scenarios broke, so there is no verdict
        about the agent at all; report that as a setup problem, never as a
        failing agent."""
        return gate_tool(target, command, runs, pass_threshold)

    return mcp


def run_stdio() -> None:
    """Entry point for ``checkpoint mcp`` — serve over stdio for an MCP client."""
    build_server().run(transport="stdio")
