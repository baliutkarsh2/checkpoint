"""Pieces every command needs: the console, the project, and shared options.

The rule this module exists to enforce: a setting means the same thing and is
resolved the same way in every command. One flag name, one env var, one place in
``checkpoint.toml``, one precedence order (:mod:`checkpoint.project`).
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import click
from rich.console import Console
from rich.markup import escape

from checkpoint.engine import Agent, RunOptions
from checkpoint.project import ConfigError, Project

console = Console()
err_console = Console(stderr=True)

F = TypeVar("F", bound=Callable[..., Any])


def plain(text: object) -> str:
    """Text that came from a scenario, an assertion or an error, made safe to print.

    Rich reads ``[...]`` as a style tag, so an assertion like
    ``github.issues[title == "x"]`` would print as ``github.issues`` — quietly
    turning a precise check into a vague one on screen. Everything Checkpoint
    did not write itself goes through here.
    """
    return escape(str(text))


def fail(message: str, *, hint: str = "", code: int = 2) -> None:
    """Print a problem the user can act on, and stop. Never raises past here."""
    err_console.print(f"[red]{plain(message)}[/red]")
    if hint:
        err_console.print(f"[dim]{plain(hint)}[/dim]")
    sys.exit(code)


def project() -> Project:
    """The loaded ``checkpoint.toml``, or an empty project when there is none.

    Any twins the project declares are registered here, before a command can
    resolve a twin name, so ``[twins.billing]`` works in a scenario exactly as
    ``github`` does.
    """
    try:
        loaded = Project.load()
        loaded.register_twins()
    except ConfigError as e:
        fail(str(e))
        raise  # unreachable; keeps type checkers honest
    return loaded


# -- the agent under test -----------------------------------------------------

_NO_AGENT = """\
Checkpoint does not know how to start your agent.

Either point at it once:
    checkpoint init --command "python my_agent.py"

or pass it for this command only:
    --command "python my_agent.py"
"""


def agent_options(f: F) -> F:
    """``--command`` and friends: how to start the agent, overriding the project."""
    options = [
        click.option("--command", "-c", default=None, metavar="CMD",
                     help="Command that runs your agent, e.g. 'python my_agent.py'. "
                          "Overrides [agent] in checkpoint.toml."),
        click.option("--url", default=None, metavar="URL",
                     help="HTTP endpoint that answers tasks, instead of a command."),
        click.option("--task-via", type=click.Choice(["env", "arg", "stdin"]), default=None,
                     help="How the agent receives the task. [default: env]"),
        click.option("--task-env", default=None, metavar="NAME",
                     help="Variable holding the task with --task-via env. "
                          "[default: CHECKPOINT_TASK]"),
        click.option("--task-arg", default=None, metavar="FLAG",
                     help="Flag the task follows with --task-via arg, e.g. --prompt."),
        click.option("--cwd", default=None, type=click.Path(exists=True, file_okay=False),
                     help="Working directory for the agent."),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def resolve_agent(proj: Project, command: str | None = None, **overrides: Any) -> Agent:
    """The agent to test, from the flags then the project. Exits if there is none."""
    try:
        agent = proj.build_agent(command, **overrides)
    except (ConfigError, ValueError) as e:
        fail(str(e))
        raise
    if agent is None:
        fail(_NO_AGENT.rstrip(), code=2)
        raise
    return agent


# -- the sandbox --------------------------------------------------------------


def sandbox_options(f: F) -> F:
    """What the agent may reach, and how the world misbehaves while it runs."""
    options = [
        click.option("--intercept/--no-intercept", default=None,
                     help="Route calls to production hostnames (https://api.github.com) into "
                          "the twins, so you test the code path you ship. [default: on]"),
        click.option("--egress", type=click.Choice(["open", "llm", "none"]), default=None,
                     help="What the agent may reach beyond the twins. [default: llm]"),
        click.option("--allow-host", "allow_hosts", multiple=True, metavar="HOST",
                     help="Also allow this host through (repeatable). Accepts *.example.com."),
        click.option("--rate-limit", type=int, default=None, metavar="N",
                     help="Refuse each twin's calls after N requests, as a real API would."),
        click.option("--read-only", is_flag=True, default=False,
                     help="Refuse every write and fail the run if the agent attempted one."),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def resolve_options(
    proj: Project,
    *,
    judge_model: str | None = None,
    timeout: float | None = None,
    intercept: bool | None = None,
    egress: str | None = None,
    allow_hosts: Sequence[str] = (),
    rate_limit: int | None = None,
    read_only: bool = False,
    **extra: Any,
) -> RunOptions:
    """Sandbox and judge settings, from the flags then the project."""
    hosts = tuple(allow_hosts) or tuple(proj.sandbox.get("allow_hosts") or ())
    return RunOptions(
        judge_model=proj.judge_model(judge_model),
        timeout=proj.agent_timeout(timeout),
        intercept=bool(proj.sandbox_setting("intercept", intercept, True)),
        egress=proj.sandbox_setting("egress", egress, "llm"),
        allow_hosts=hosts,
        faults={"*": {"rate_limit": rate_limit}} if rate_limit is not None else {},
        read_only=read_only,
        **extra,
    )


# -- scenarios ----------------------------------------------------------------


def resolve_targets(proj: Project, targets: Sequence[str]) -> list[Path]:
    """Scenario files named on the command line, or the project's scenario paths.

    Directories expand to the ``.md`` files under them. A named path that does
    not exist is an error; an empty project directory is reported as such.
    """
    roots = [Path(t) for t in targets] if targets else proj.scenario_paths()
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
        elif root.is_file():
            files.append(root)
        elif targets:
            fail(f"no such scenario: {root}")
        else:
            fail(f"no scenarios found: {root} does not exist",
                 hint="Create one with `checkpoint new \"<what the agent should do>\"`.")
    if not files:
        where = ", ".join(str(r) for r in roots)
        fail(f"no scenario files (*.md) under {where}",
             hint="Create one with `checkpoint new \"<what the agent should do>\"`.")
    # A file passed twice — say `scenarios` and `scenarios/one.md` — runs once.
    seen: dict[Path, None] = {}
    for f in files:
        seen.setdefault(f.resolve(), None)
    return list(seen)


# -- rendering ----------------------------------------------------------------


def _unicode_ok() -> bool:
    """Whether this stdout can carry the marks — a redirected Windows pipe cannot."""
    encoding = getattr(sys.stdout, "encoding", "") or "ascii"
    try:
        "✓✗".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


_MARKS = ({"pass": "✓", "fail": "✗", "error": "!"} if _unicode_ok()
          else {"pass": "PASS", "fail": "FAIL", "error": "ERR"})
_MARK_STYLES = {"pass": "green", "fail": "red", "error": "magenta"}


def mark(status: str) -> str:
    """A criterion's verdict, as rich markup."""
    style = _MARK_STYLES.get(status, "dim")
    return f"[{style}]{_MARKS.get(status, status)}[/{style}]"


def score_color(score: float) -> str:
    return "green" if score >= 100 else ("yellow" if score >= 50 else "red")


def verdict_color(verdict: str) -> str:
    return {
        "SHIP": "green", "BLOCK": "red", "ERROR": "red",
        "CONDITIONAL": "yellow", "INCONCLUSIVE": "yellow",
    }.get(verdict, "white")
