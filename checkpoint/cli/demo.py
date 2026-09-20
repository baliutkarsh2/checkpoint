"""``checkpoint demo`` — the thirty-second answer to "does this thing work?".

A tiny standard-library agent files an issue against a real GitHub twin, and the
result is scored by assertions alone. No API key, no Docker, no network: if this
prints a score, Checkpoint works on this machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.panel import Panel

from ._shared import console

_NEXT = """\
Nothing there was mocked. The agent made real HTTP calls,
a real GitHub twin changed state, and every criterion was
checked against that state, not against what the agent
said it did.

Point it at your own agent:

  [bold]checkpoint init --command "python my_agent.py"[/bold]
  [bold]checkpoint run[/bold]
"""


@click.command("demo")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Stream the demo agent's own output while it runs.")
def demo(verbose):
    """Run a bundled agent against a bundled scenario. No setup, no API key.

    Everything stays on this machine: the twin runs in this process, the agent
    is a 60-line script in the standard library, and every criterion is decided
    by an assertion rather than a model.
    """
    from checkpoint.engine import Agent, RunOptions, run_scenario
    from checkpoint.scenario import parse_file

    from .run import _print_result

    scenario = parse_file(Path(__file__).parent.parent / "demo" / "smoke-scenario.md")
    # Invoked as a module, never as a file path: the demo lives in site-packages,
    # whose path routinely contains spaces that command splitting would break on.
    agent = Agent(command=[sys.executable, "-m", "checkpoint.demo.harness_fake"],
                  name="checkpoint demo agent")

    console.print()
    console.print(f"[bold]{scenario.title}[/bold]  [dim]github · no API key · no network[/dim]")
    result = run_scenario(scenario, agent, options=RunOptions(
        egress="none", on_line=_echo if verbose else None))
    _print_result(result)

    console.print(Panel.fit(_NEXT.rstrip(), border_style="green", title="next"))
    sys.exit(0 if result.score == 100 else 1)


def _echo(stream: str, line: str) -> None:
    console.print(f"[dim]  {line.rstrip()}[/dim]", highlight=False)
