"""Checkpoint — prove an agent works before customers find out it doesn't.

Everything the ``checkpoint`` command does is available here, in the same
vocabulary, so a notebook, a pytest suite or your own tooling drives the same
engine the CLI does:

    from checkpoint import Agent, RunOptions, parse_file, run_scenario

    scenario = parse_file("scenarios/refund.md")
    result = run_scenario(scenario, Agent(command="python my_agent.py"))
    print(result.score, [c.text for c in result.criteria if not c.passed])

To drive the twins yourself — no scenario, no agent — start a sandbox and talk
to them over HTTP or MCP:

    from checkpoint import Sandbox

    with Sandbox(["github", "slack"]) as sandbox:
        env = sandbox.agent_env()      # credentials and URLs an SDK expects
        ...
        print(sandbox.views())         # what changed while you worked

Names resolve on first use rather than at import, so ``import checkpoint`` costs
almost nothing and ``checkpoint --help`` stays instant.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "0.1.0"

_EXPORTS = {
    "Agent": ("checkpoint.engine", "Agent"),
    "RunOptions": ("checkpoint.engine", "RunOptions"),
    "Sandbox": ("checkpoint.engine", "Sandbox"),
    "SandboxError": ("checkpoint.engine", "SandboxError"),
    "run_scenario": ("checkpoint.engine", "run_scenario"),
    "Project": ("checkpoint.project", "Project"),
    "CriterionResult": ("checkpoint.runner", "CriterionResult"),
    "RunResult": ("checkpoint.runner", "RunResult"),
    "Criterion": ("checkpoint.scenario", "Criterion"),
    "Scenario": ("checkpoint.scenario", "Scenario"),
    "parse": ("checkpoint.scenario", "parse"),
    "parse_file": ("checkpoint.scenario", "parse_file"),
}

__all__ = [*sorted(_EXPORTS), "__version__"]

if TYPE_CHECKING:  # the same names, for type checkers and editors
    from .engine import Agent as Agent
    from .engine import RunOptions as RunOptions
    from .engine import Sandbox as Sandbox
    from .engine import SandboxError as SandboxError
    from .engine import run_scenario as run_scenario
    from .project import Project as Project
    from .runner import CriterionResult as CriterionResult
    from .runner import RunResult as RunResult
    from .scenario import Criterion as Criterion
    from .scenario import Scenario as Scenario
    from .scenario import parse as parse
    from .scenario import parse_file as parse_file


def __getattr__(name: str) -> object:
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'checkpoint' has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module), attribute)
    globals()[name] = value  # bind it, so the lookup happens once
    return value


def __dir__() -> list[str]:
    return list(__all__)
