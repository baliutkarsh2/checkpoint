"""``checkpoint twins`` — the services your scenarios run against.

A twin is an HTTP server that speaks a production API (GitHub, Slack, Stripe,
...) and keeps state, so a scenario can check what your agent actually did
rather than what it said it did. ``checkpoint run`` starts the twins a scenario
names and throws them away when the run ends; the commands here keep one
running for as long as you want, to build against by hand.
"""
from __future__ import annotations

import json

import click
from rich import box
from rich.table import Table

from checkpoint.twins import registry

from ._shared import console, fail


@click.group("twins")
def twins() -> None:
    """The services scenarios run against.

    \b
        checkpoint twins list           # every twin, and what it simulates
        checkpoint twins start github   # one that outlives this command
        checkpoint twins status         # what is running right now

    A running twin serves its API at a local URL and the same operations over
    MCP. Nothing it stores is real, and nothing it does leaves this machine.
    """


@twins.command("list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON array and nothing else.")
def list_twins(as_json: bool) -> None:
    """Every twin Checkpoint can run.

    Name one in a scenario's ``twins:`` line and it is started for that run.
    Calls to the hostnames under Intercepts are routed into the twin, so the
    SDK and the code path you ship are the ones under test.
    """
    specs = registry.all_specs()
    if as_json:
        click.echo(json.dumps([{
            "name": spec.name,
            "title": spec.title,
            "domains": list(spec.domains),
            "token_env": list(spec.token_env),
            "docs": spec.docs,
            "seeds": spec.seed_names(),
        } for spec in specs], indent=2))
        return

    # No "Simulates" column: for every twin it repeats the name back ("github"
    # simulates "GitHub"), and the width it costs is width the seed list needs.
    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Twin", style="bold")
    table.add_column("Intercepts")
    table.add_column("Seeds")
    for spec in specs:
        # A hostname has no spaces to wrap at, so a full list shreds the column
        # on an 80-column terminal. The first one identifies the service; --json
        # has all of them for anything that needs the complete set.
        rest = len(spec.domains) - 1
        intercepts = (spec.domains[0] if spec.domains else "-")
        if rest > 0:
            intercepts += f" [dim]+{rest}[/dim]"
        table.add_row(spec.name, intercepts, ", ".join(spec.seed_names()) or "-")
    console.print(table)
    console.print("[dim]A seed is the state a twin starts from: "
                  "`checkpoint twins start github --seed large-backlog`. "
                  "`--json` lists every hostname.[/dim]")


@twins.command("start")
@click.argument("name")
@click.option("--seed", "seed_name", default=None, metavar="NAME",
              help="Load this dataset once the twin is up. "
                   "[default: the twin's empty state]")
@click.option("--ttl", "ttl_seconds", type=int, default=None, metavar="SECONDS",
              help="Note when this twin should be thrown away. Advisory: "
                   "`twins status` shows it, nothing stops the twin for you.")
def start_twin(name: str, seed_name: str | None, ttl_seconds: int | None) -> None:
    """Start a twin and leave it running.

    NAME is a twin from `checkpoint twins list`. It keeps running after this
    command exits, so you can point your agent at the URL it prints and work
    against real responses and real stored state for as long as you like.

    \b
        checkpoint twins start github --seed large-backlog
        checkpoint twins status github
        checkpoint twins stop github
    """
    from checkpoint.twins import sessions

    spec = _spec(name)
    _check_seed(spec, seed_name)

    running = next((t for t in sessions.list_all()
                    if t.get("id") == spec.name and t.get("alive")), None)
    if running:
        fail(f"the {spec.name} twin is already running at {running.get('url')} "
             f"(pid {running.get('pid')})",
             hint=f"Stop it first: checkpoint twins stop {spec.name}", code=1)

    try:
        entry = sessions.start(spec.name)
    except RuntimeError:
        fail(f"the {spec.name} twin never answered after starting",
             hint="`checkpoint doctor` checks this machine's ports and Python.", code=1)

    if ttl_seconds:
        try:
            entry = sessions.renew(spec.name, ttl_seconds=ttl_seconds)
        except (KeyError, RuntimeError):
            pass  # The twin is up; only the expiry note failed to stick.

    console.print(f"[green]{spec.title} twin running[/green] [dim]pid {entry['pid']}[/dim]")
    console.print(f"  [dim]URL[/dim]         {entry['url']}")
    console.print(f"  [dim]MCP[/dim]         {entry['mcp_url']}")
    holder = f"  [dim]({spec.token_env[0]})[/dim]" if spec.token_env else ""
    console.print(f"  [dim]credential[/dim]  {entry.get('token') or '(none)'}{holder}")
    if entry.get("expires_at_iso"):
        console.print(f"  [dim]expires[/dim]     {entry['expires_at_iso']}")

    if seed_name:
        result = sessions.seed(spec.name, seed_name)
        if not result.get("ok"):
            fail(f"the twin is running, but the seed {seed_name!r} did not load: "
                 f"{_why(result)}",
                 hint=f"Try it again: checkpoint twins seed {spec.name} {seed_name}",
                 code=1)
        console.print(f"  [dim]seed[/dim]        {seed_name}")

    console.print(f"[dim]Stop it with `checkpoint twins stop {spec.name}`.[/dim]")


@twins.command("stop")
@click.argument("name", required=False)
@click.option("--all", "stop_all", is_flag=True, default=False,
              help="Stop every twin that is running.")
def stop_twin(name: str | None, stop_all: bool) -> None:
    """Stop a running twin and discard everything it holds.

    \b
        checkpoint twins stop github
        checkpoint twins stop --all
    """
    from checkpoint.twins import sessions

    if stop_all and name:
        raise click.UsageError("name a twin or pass --all, not both")
    if not stop_all and not name:
        raise click.UsageError("name a twin to stop, or pass --all")

    if stop_all:
        running = [str(t.get("id")) for t in sessions.list_all() if t.get("alive")]
        if not running:
            console.print("[dim]No twins are running.[/dim]")
            return
        for twin_name in running:
            sessions.stop(twin_name)
            console.print(f"[green]Stopped {twin_name}.[/green]")
        return

    spec = _spec(name or "")
    if sessions.stop(spec.name):
        console.print(f"[green]Stopped {spec.name}.[/green]")
    else:
        console.print(f"[dim]The {spec.name} twin was not running.[/dim]")


@twins.command("status")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON document and nothing else.")
def twin_status(name: str | None, as_json: bool) -> None:
    """What is running: every twin, or one in detail.

    With no NAME, the twins you started that are still alive. With one, how
    long it has been up, how many requests have reached it, and how much state
    it is holding.
    """
    from checkpoint.twins import sessions

    if name is None:
        rows = [t for t in sessions.list_all() if t.get("alive")]
        if as_json:
            click.echo(json.dumps(rows, indent=2, default=str))
            return
        if not rows:
            console.print("[dim]No twins are running. "
                          "Start one with `checkpoint twins start github`.[/dim]")
            return
        table = Table(box=box.SIMPLE, show_edge=False)
        table.add_column("Twin", style="bold")
        table.add_column("URL", overflow="fold")
        table.add_column("PID", justify="right")
        table.add_column("Started")
        table.add_column("Expires")
        for row in rows:
            table.add_row(str(row.get("id", "?")), str(row.get("url", "?")),
                          str(row.get("pid", "?")), str(row.get("started_at", "?")),
                          str(row.get("expires_at_iso") or "-"))
        console.print(table)
        return

    spec = _spec(name)
    info = sessions.inspect(spec.name)
    if info is None:
        fail(f"the {spec.name} twin is not running",
             hint=f"Start it with `checkpoint twins start {spec.name}`.", code=1)
        return
    if not info.get("alive"):
        fail(f"the {spec.name} twin is registered but its process is gone",
             hint=f"Its entry is cleared; start it again with "
                  f"`checkpoint twins start {spec.name}`.", code=1)
        return
    if as_json:
        click.echo(json.dumps(info, indent=2, default=str))
        return

    console.print(f"[bold]{spec.title} twin[/bold] [dim]pid {info.get('pid')}[/dim]")
    console.print(f"  [dim]URL[/dim]        {info.get('url')}")
    console.print(f"  [dim]MCP[/dim]        {info.get('mcp_url')}")
    console.print(f"  [dim]started[/dim]    {info.get('started_at', '?')}")
    if info.get("expires_at_iso"):
        console.print(f"  [dim]expires[/dim]    {info['expires_at_iso']}")
    console.print(f"  [dim]requests[/dim]   {info.get('request_count', 0)}")
    # Keys starting with an underscore are the twin's own knobs, not collections.
    holding = ", ".join(k for k in (info.get("state_keys") or [])
                        if not k.startswith("_")) or "nothing yet"
    console.print(f"  [dim]holding[/dim]    {holding} "
                  f"[dim]({info.get('state_size', 0)} bytes)[/dim]")
    if info.get("state_error"):
        console.print(f"  [yellow]its state could not be read: {info['state_error']}[/yellow]")


@twins.command("seed")
@click.argument("name")
@click.argument("seed")
def seed_twin(name: str, seed: str) -> None:
    """Load a named dataset into a running twin.

    SEED is one of the datasets `checkpoint twins list` shows for this twin:
    ``large-backlog`` for a repository with hundreds of issues, ``empty`` for
    nothing at all. Seeding replaces whatever the twin is holding, so your next
    attempt starts from a state you chose instead of the last one's leftovers.
    """
    from checkpoint.twins import sessions

    spec = _spec(name)
    _check_seed(spec, seed)
    result = _on_running(sessions.seed, spec, seed)
    if not result.get("ok"):
        fail(f"could not load {seed!r} into the {spec.name} twin: {_why(result)}", code=1)
    console.print(f"[green]Loaded {seed} into the {spec.name} twin.[/green]")


@twins.command("reset")
@click.argument("name")
def reset_twin(name: str) -> None:
    """Put a running twin back to its factory state.

    Everything the agent created, changed or deleted is discarded and the
    request log starts over, without stopping the twin or changing its URL.
    """
    from checkpoint.twins import sessions

    spec = _spec(name)
    result = _on_running(sessions.reset, spec)
    if not result.get("ok"):
        fail(f"could not reset the {spec.name} twin: {_why(result)}", code=1)
    console.print(f"[green]Reset the {spec.name} twin.[/green]")


@twins.command("tools")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON array and nothing else.")
def twin_tools(name: str, as_json: bool) -> None:
    """The MCP tools a running twin exposes.

    An agent that speaks MCP can drive the twin through these instead of HTTP.
    They are the same operations the API offers, named and described for a
    model to choose between.
    """
    from checkpoint.twins import sessions

    spec = _spec(name)
    result = _on_running(sessions.tools, spec)
    found = result.get("tools") or []
    if as_json:
        click.echo(json.dumps(found, indent=2, default=str))
        return
    if not found:
        why = "" if result.get("ok") else f": {_why(result)}"
        console.print(f"[dim]The {spec.name} twin exposes no MCP tools{why}.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Tool", style="bold")
    table.add_column("Does", overflow="fold")
    for tool in found:
        table.add_row(str(tool.get("name", "?")), str(tool.get("description", "")))
    console.print(table)
    console.print(f"[dim]{len(found)} tools · `checkpoint twins status {spec.name}` "
                  f"prints the MCP URL to point an agent at.[/dim]")


# -- shared --------------------------------------------------------------------


def _spec(name: str) -> registry.TwinSpec:
    """The twin NAME means, or an error naming the ones that exist."""
    try:
        return registry.get(name)
    except registry.UnknownTwinError as e:
        fail(str(e), hint="`checkpoint twins list` describes each one.")
        raise  # unreachable; keeps type checkers honest


def _check_seed(spec: registry.TwinSpec, seed_name: str | None) -> None:
    """Catch a mistyped seed here, where this twin's own list can be shown."""
    available = spec.seed_names()
    if seed_name and available and seed_name not in available:
        fail(f"the {spec.name} twin has no seed called {seed_name!r}",
             hint=f"Available: {', '.join(available)}")


def _on_running(call, spec: registry.TwinSpec, *args) -> dict:
    """Ask a running twin to do something. Exits when it is not running."""
    try:
        return call(spec.name, *args)
    except KeyError:
        fail(f"the {spec.name} twin is not running",
             hint=f"Start it with `checkpoint twins start {spec.name}`.", code=1)
        raise  # unreachable; keeps type checkers honest
    except RuntimeError:
        fail(f"the {spec.name} twin is registered but its process is gone",
             hint=f"Start it again with `checkpoint twins start {spec.name}`.", code=1)
        raise  # unreachable; keeps type checkers honest


def _why(result: dict) -> str:
    """What a twin said when it refused, in as few words as it gave us."""
    return str(result.get("error") or f"HTTP {result.get('status', '?')}")
