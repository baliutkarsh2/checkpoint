"""``checkpoint view`` and ``checkpoint mcp`` — two ways to watch from outside.

The dashboard reads the runs already on disk: what the agent did call, which
criteria failed and why, and how the pass rate is moving between builds. The MCP
server puts the same machinery inside a coding agent, so it can run a scenario
or gate a build while it is still writing the code under test.
"""
from __future__ import annotations

import os
from pathlib import Path

import click
from rich.panel import Panel

from ._shared import console, fail, project

_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# The dashboard starts agent processes on request (POST /api/jobs). On loopback
# that is a single-user local tool; on any other interface it is remote code
# execution for anyone who can reach the port.
_NO_KEY = """\
Binding off loopback exposes POST /api/jobs, which runs agent commands, to
everyone who can reach the port. Set a key first:

    export CHECKPOINT_DASHBOARD_API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')

or leave --host at 127.0.0.1.
"""


@click.command("view")
@click.option("--port", type=int, default=4001, show_default=True,
              help="Port to listen on.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Interface to bind. Off loopback, CHECKPOINT_DASHBOARD_API_KEY is required.")
@click.option("--scenarios", "scenarios_dir", type=click.Path(file_okay=False), default=None,
              metavar="DIR",
              help="Directory of scenarios to show. [default: the project's scenarios]")
@click.option("--open/--no-open", "auto_open", default=False,
              help="Open the dashboard in your browser.")
@click.option("--model", default=None, metavar="MODEL",
              help="Judge model the dashboard offers for new runs.")
def view(port, host, scenarios_dir, auto_open, model):
    """Open the dashboard on this machine.

    Every run Checkpoint has recorded, with the trajectory behind each score:
    the calls the agent made to the twins, the state it left, the criteria that
    failed and the judge's reasoning — plus the pass rate over time, which is
    the part a single run cannot show you.

    \b
        checkpoint view --open

    It binds to 127.0.0.1 because it can start agent processes on request.
    """
    if host not in _LOOPBACK and not os.environ.get("CHECKPOINT_DASHBOARD_API_KEY"):
        fail(f"refusing to serve on {host!r} without authentication", hint=_NO_KEY, code=1)

    import logging
    import webbrowser

    import uvicorn

    from checkpoint.dashboard.app import create_app
    from checkpoint.run_record import RUNS_DIR
    from checkpoint.twins.sessions import SESSIONS_FILE

    proj = project()
    scenarios = Path(scenarios_dir) if scenarios_dir else proj.scenario_paths()[0]
    scenarios = scenarios.resolve()

    # The middleware and the filesystem watcher log through stdlib logging;
    # configuring it here puts their lines under uvicorn's handler instead of
    # dropping them.
    logging.basicConfig(
        level=os.environ.get("CHECKPOINT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    app = create_app(
        runs_dir=RUNS_DIR,
        scenarios_dir=scenarios,
        twin_sessions_file=SESSIONS_FILE,
        project_dir=proj.root,
        judge_model_default=proj.judge_model(model),
    )
    url = f"http://{host}:{port}"
    console.print(Panel.fit(
        f"[bold]Dashboard:[/bold]  {url}\n"
        f"[bold]API docs:[/bold]   {url}/api/docs\n"
        f"[dim]Runs:[/dim]        {RUNS_DIR.resolve()}\n"
        f"[dim]Scenarios:[/dim]   {scenarios}\n\n"
        "Press Ctrl-C to stop.",
        title="checkpoint view", border_style="green"))
    if auto_open:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="warning")


@click.command("mcp")
def mcp():
    """Serve Checkpoint over MCP (stdio), for the agent writing your agent.

    A coding agent that speaks MCP can list your scenarios, run one, and gate a
    build without leaving its own loop — so it checks its work against the same
    verdict your CI will use, before it tells you it is done.

    \b
    Register it with your MCP client:
        {"mcpServers": {"checkpoint": {"command": "checkpoint", "args": ["mcp"]}}}

    Nothing is printed — stdout is the MCP transport.
    """
    from checkpoint.mcp_gate.server import run_stdio

    run_stdio()
