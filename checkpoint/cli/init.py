"""``checkpoint init`` — point Checkpoint at the agent you already have."""
from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.panel import Panel

from checkpoint.project import CONFIG_NAME

from ._shared import console, fail

# What an agent's entry point usually looks like, best guess first. Offered as
# the default answer so the common case is one Enter key.
_LIKELY = (
    ("main.py", "python main.py"),
    ("agent.py", "python agent.py"),
    ("app.py", "python app.py"),
    ("src/main.py", "python src/main.py"),
    ("src/agent.py", "python src/agent.py"),
    ("index.js", "node index.js"),
    ("agent.js", "node agent.js"),
    ("src/index.ts", "npx tsx src/index.ts"),
)

_PROMPT = """\
[bold]How do you run your agent?[/bold]

Checkpoint runs this command once per scenario. It puts the task in
[cyan]$CHECKPOINT_TASK[/cyan] and reads the final answer from stdout — so an agent that
already works from the command line needs no changes at all.
"""


@click.command("init")
@click.argument("target_dir", required=False, type=click.Path(file_okay=False), default=".")
@click.option("--command", "-c", default=None, metavar="CMD",
              help="The command that runs your agent, e.g. 'python my_agent.py'. "
                   "Asked for interactively when omitted.")
@click.option("--task-via", type=click.Choice(["env", "arg", "stdin"]), default="env",
              show_default=True, help="How your agent receives the task.")
@click.option("--task-arg", default=None, metavar="FLAG",
              help="Flag the task follows with --task-via arg, e.g. --prompt.")
@click.option("--model", default=None, metavar="MODEL",
              help="Judge model for [P] criteria. [default: gpt-5.6-luna]")
@click.option("--ci/--no-ci", default=None,
              help="Write a GitHub Actions workflow that gates every pull request. "
                   "[default: only if the repo already has .github/]")
@click.option("--skill/--no-skill", default=None,
              help="Write a .claude/skills/ skill file so your coding agent can drive "
                   "Checkpoint. [default: only if the repo already has .claude/]")
def init(target_dir, command, task_via, task_arg, model, ci, skill):
    """Set up Checkpoint in this repository.

    Writes checkpoint.toml and a starter scenario. No harness, no wrapper, no
    changes to your agent's code — and nothing that already exists is touched.

    \b
        checkpoint init --command "python my_agent.py"
    """
    from checkpoint import init as scaffolding
    from checkpoint.llm import DEFAULT_MODEL

    target = Path(target_dir)
    if (target / CONFIG_NAME).is_file() and not command:
        console.print(f"[dim]{target / CONFIG_NAME} already exists — keeping it.[/dim]")

    command = command or _ask(target)
    try:
        result = scaffolding.scaffold(
            target, command=command, task_via=task_via, task_arg=task_arg,
            model=model or DEFAULT_MODEL, ci=ci, skill=skill)
    except (OSError, FileNotFoundError) as e:
        fail(f"could not set up {target}: {e}")
        return

    for rel in result.created:
        console.print(f"  [green]+[/green] {rel}")
    for rel in result.kept:
        console.print(f"  [dim]= {rel} (kept)[/dim]")

    console.print(Panel.fit(
        "\n".join(result.next_steps),
        title="[bold]ready[/bold]" if result.created else "[bold]nothing to do[/bold]",
        border_style="green"))


def _ask(target: Path) -> str:
    """Ask how to start the agent, defaulting to whatever the repo looks like."""
    default = _guess(target)
    if not sys.stdin.isatty():
        if default:
            console.print(f"[dim]Using the command that fits this repo: {default}[/dim]")
            return default
        fail("pass --command with the command that runs your agent",
             hint='For example: checkpoint init --command "python my_agent.py"')
    console.print(Panel.fit(_PROMPT.rstrip(), border_style="cyan"))
    answer = click.prompt("  command", default=default or "python my_agent.py").strip()
    if not answer:
        fail("a command is required")
    return answer


def _guess(target: Path) -> str | None:
    for relative, command in _LIKELY:
        if (target / relative).is_file():
            return command
    return None
