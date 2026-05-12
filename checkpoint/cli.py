"""checkpoint CLI."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import click
from dotenv import find_dotenv, load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Walk up from cwd to find a .env (e.g. ~/projects/.env). Don't override
# anything already in the environment.
load_dotenv(find_dotenv(usecwd=True), override=False)

from .runner import RunResult, run_once
from .scenario import Scenario, parse_file

console = Console()


@click.group()
def main():
    """checkpoint: agent testing against stateful SaaS twins."""


@main.command()
@click.argument("scenario_path", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--harness", required=True, help="Shell command that runs the agent harness. e.g. 'python harness.py'")
@click.option("--task", default=None, help="Inline task (overrides scenario prompt; works without a scenario file).")
@click.option("--clone", default=None, help="Clone to use. Only 'github' is supported.")
@click.option("--runs", type=int, default=None, help="Override number of runs.")
@click.option("--model", default="gpt-4o-mini", help="OpenAI model for the LLM judge.")
@click.option("--timeout", type=int, default=None, help="Override harness timeout (seconds).")
@click.option("--cwd", type=click.Path(exists=True, file_okay=False), default=None, help="Working dir for the harness.")
@click.option("--trace-out", type=click.Path(dir_okay=False), default=None, help="Save all-runs trace+state JSON to file.")
def run(scenario_path, harness, task, clone, runs, model, timeout, cwd, trace_out):
    """Run a scenario or inline --task against the agent harness."""
    if not scenario_path and not task:
        console.print("[red]Provide a scenario file or --task[/red]")
        sys.exit(2)

    scenario = parse_file(scenario_path) if scenario_path else Scenario()
    if task:
        scenario.prompt = task
    if clone:
        scenario.config["clones"] = clone
    if runs is not None:
        scenario.config["runs"] = str(runs)
    if timeout is not None:
        scenario.config["timeout"] = str(timeout)
    if not scenario.clones:
        scenario.config["clones"] = "github"
    if not scenario.prompt:
        console.print("[red]Scenario has no Prompt section and no --task given[/red]")
        sys.exit(2)

    harness_cmd = shlex.split(harness)

    console.print(Panel.fit(
        f"[bold]{scenario.title or 'Untitled scenario'}[/bold]\n"
        f"[dim]clone:[/dim] {', '.join(scenario.clones)}\n"
        f"[dim]runs:[/dim]  {scenario.runs}\n"
        f"[dim]judge:[/dim] {model}",
        title="checkpoint run",
        border_style="cyan",
    ))

    if scenario.prompt:
        preview = scenario.prompt[:240]
        suffix = "…" if len(scenario.prompt) > 240 else ""
        console.print(f"[dim]Task:[/dim] {preview}{suffix}")

    results: list[RunResult] = []
    for i in range(scenario.runs):
        console.print(f"\n[bold]Run {i + 1}/{scenario.runs}[/bold]")
        r = run_once(scenario, harness_cmd, cwd=cwd, judge_model=model)
        results.append(r)
        _print_run(r)

    if scenario.runs > 1:
        _print_summary(results)

    if trace_out:
        Path(trace_out).write_text(json.dumps([_dump(r) for r in results], indent=2))
        console.print(f"[dim]Trace written to {trace_out}[/dim]")

    if not all(r.complete and r.score == 100.0 for r in results):
        sys.exit(1)


def _dump(r: RunResult) -> dict:
    return {
        "final_answer": r.final_answer,
        "exit_code": r.exit_code,
        "error": r.error,
        "score": r.score,
        "trace": r.trace,
        "state": r.state,
        "criteria": [c.__dict__ for c in r.criteria],
    }


def _print_run(r: RunResult) -> None:
    if r.error:
        console.print(f"[red]Error: {r.error}[/red]")
        if r.stderr:
            console.print(Panel(r.stderr, title="stderr (tail)", border_style="red"))

    if r.criteria:
        t = Table(box=box.SIMPLE_HEAD, show_lines=False)
        t.add_column("", style="dim", width=2)
        t.add_column("Kind", width=4)
        t.add_column("Criterion")
        t.add_column("Reasoning", overflow="fold")
        t.add_column("Eval", style="dim", width=14)
        for c in r.criteria:
            mark = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
            t.add_row(mark, f"[{c.kind}]", c.text, c.reasoning, c.evaluator)
        console.print(t)

    color = "green" if r.score == 100 else ("yellow" if r.score >= 50 else "red")
    line = f"Score: [{color}]{r.score:.0f}/100[/{color}]  ({len(r.trace)} API call(s))"
    if not r.complete:
        line += "  [red]INCOMPLETE[/red]"
    console.print(line)


def _print_summary(results: list[RunResult]) -> None:
    completed = [r for r in results if r.complete]
    if not completed:
        console.print("\n[red]All runs incomplete. No satisfaction score.[/red]")
        return
    avg = sum(r.score for r in completed) / len(completed)
    color = "green" if avg == 100 else ("yellow" if avg >= 70 else "red")
    console.print(Panel.fit(
        f"[bold]Satisfaction: [{color}]{avg:.1f}/100[/{color}][/bold]\n"
        f"[dim]{len(completed)}/{len(results)} runs complete[/dim]",
        title="Summary",
        border_style=color,
    ))


if __name__ == "__main__":
    main()
