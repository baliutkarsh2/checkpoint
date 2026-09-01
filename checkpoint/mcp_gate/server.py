"""FastMCP server exposing Checkpoint's testing tools over stdio."""
from __future__ import annotations

from checkpoint.mcp_compat import FastMCP, make_server

from .tools import gate_tool, list_scenarios_tool, run_scenario_tool


def build_server() -> FastMCP:
    mcp = make_server("checkpoint")

    @mcp.tool()
    def list_scenarios(scenarios_dir: str = "scenarios") -> list[dict]:
        """List Checkpoint test scenarios under a directory, with each one's
        prompt, criteria count, and target twins."""
        return list_scenarios_tool(scenarios_dir)

    @mcp.tool()
    def run_scenario(scenario_path: str, harness: str,
                     judge_model: str = "gpt-4o-mini") -> dict:
        """Run one scenario against the agent. `harness` is the command that runs
        the agent (e.g. 'python my_agent.py'). Returns the 0-100 score and each
        criterion's pass/fail with reasoning."""
        return run_scenario_tool(scenario_path, harness, judge_model)

    @mcp.tool()
    def gate(target: str, harness: str, runs: int = 10,
             pass_threshold: float = 80.0) -> dict:
        """Statistically gate a scenario or directory: run each `runs` times and
        return a SHIP / CONDITIONAL / BLOCK verdict with per-scenario pass-rate
        confidence intervals. Use before shipping an agent change."""
        return gate_tool(target, harness, runs, pass_threshold)

    return mcp


def run_stdio() -> None:
    """Entry point for `checkpoint mcp` — serve over stdio for an MCP client."""
    build_server().run(transport="stdio")
