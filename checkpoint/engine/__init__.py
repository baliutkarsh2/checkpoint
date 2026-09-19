"""The execution engine: run an agent against a scenario inside a sandbox.

    from checkpoint.engine import Agent, Sandbox, run_scenario

    agent = Agent(command="python my_agent.py")
    result = run_scenario(scenario, agent)
"""
from .agent import Agent, AgentOutput, extract_answer, split_command
from .run import RunOptions, run_scenario, scenario_setups, scenario_twins
from .sandbox import Sandbox, SandboxError, TwinSetup

__all__ = [
    "Agent",
    "AgentOutput",
    "RunOptions",
    "Sandbox",
    "SandboxError",
    "TwinSetup",
    "extract_answer",
    "run_scenario",
    "scenario_setups",
    "scenario_twins",
    "split_command",
]
