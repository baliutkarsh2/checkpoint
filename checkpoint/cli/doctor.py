"""``checkpoint doctor`` — can this machine run a scenario, and if not, why."""
from __future__ import annotations

import sys

import click
from rich import box
from rich.table import Table

from ._shared import console


@click.command("doctor")
@click.option("--quick", is_flag=True, default=False,
              help="Skip starting a twin. Faster, and less conclusive.")
def doctor(quick):
    """Check that everything a run needs is working here.

    Rows marked optional are reported but never fail the command: a judge model
    key, for instance, is only needed by [P] criteria. Exits 1 if anything a run
    actually needs is broken.
    """
    from checkpoint import diagnostics

    checks = diagnostics.run_checks(include_twins=not quick)
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    table.add_column("", width=4)
    table.add_column("Check")
    table.add_column("", overflow="fold")
    for check in checks:
        if check.ok:
            status = "[green]ok[/green]"
        elif check.required:
            status = "[red]no[/red]"
        else:
            status = "[yellow]--[/yellow]"
        name = check.name if check.required else f"{check.name} [dim](optional)[/dim]"
        table.add_row(status, name, check.detail)
    console.print(table)

    problems = [c for c in checks if not c.ok and c.fix]
    if problems:
        console.print()
        for check in problems:
            label = "[red]fix[/red]" if check.required else "[dim]note[/dim]"
            console.print(f"  {label} {check.name}: {check.fix}")

    if diagnostics.all_passed(checks):
        console.print("\n[green]Ready.[/green] [dim]Try `checkpoint demo`.[/dim]")
        sys.exit(0)
    sys.exit(1)
