"""The ``checkpoint`` command.

Fifteen commands, grouped by what you are trying to do. Each lives in its own
module and is imported only when it runs, so ``checkpoint --help`` stays instant
even though the dashboard and the MCP server are one command away.
"""
from __future__ import annotations

import importlib
import sys
from typing import NamedTuple

import click
from dotenv import find_dotenv, load_dotenv

# Agents keep their provider keys in a .env beside their code. Load it before
# any command runs, without overriding what the environment already says.
load_dotenv(find_dotenv(usecwd=True), override=False)

# Windows picks the legacy code page for a redirected stream, so piping output
# to a file or a CI log would mangle every dash and check mark. Ask for UTF-8;
# where the stream cannot be reconfigured, the marks fall back to ASCII.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except (OSError, ValueError):  # pragma: no cover — a closed or exotic stream
            pass


class Entry(NamedTuple):
    name: str
    target: str
    """``module:attribute`` inside this package."""
    help: str


SECTIONS: list[tuple[str, list[Entry]]] = [
    ("Start here", [
        Entry("init", "init:init", "Point Checkpoint at your agent"),
        Entry("demo", "demo:demo", "See it work — offline, no API key"),
        Entry("run", "run:run", "Run scenarios against your agent"),
        Entry("gate", "gate:gate", "Decide whether this build ships"),
    ]),
    ("Test deeper", [
        Entry("redteam", "redteam:redteam", "Run adversarial scenarios"),
        Entry("simulate", "simulate:simulate", "Hold a conversation as a simulated user"),
    ]),
    ("Author", [
        Entry("new", "author:new", "Write a new scenario"),
        Entry("check", "author:check", "Check scenarios before you run them"),
        Entry("twins", "twins:twins", "The services scenarios run against"),
    ]),
    ("Evidence", [
        Entry("cert", "cert:cert", "Issue and verify signed verdicts"),
        Entry("report", "cert:report", "Build an assurance report"),
        Entry("runs", "runs:runs", "Past runs: list, show, compare, export"),
    ]),
    ("Tools", [
        Entry("view", "view:view", "Open the dashboard"),
        Entry("mcp", "view:mcp", "Serve Checkpoint over MCP"),
        Entry("doctor", "doctor:doctor", "Check this machine"),
    ]),
]

_ENTRIES = {entry.name: entry for _, entries in SECTIONS for entry in entries}


class CheckpointCLI(click.Group):
    """Loads each command's module on first use and lists them in sections."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(_ENTRIES)

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        entry = _ENTRIES.get(name)
        if entry is None:
            return None
        module_name, _, attr = entry.target.partition(":")
        module = importlib.import_module(f".{module_name}", __package__)
        return getattr(module, attr)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Read from the table, never by importing every command, so `--help`
        # costs one import instead of fifteen.
        for title, entries in SECTIONS:
            with formatter.section(title):
                formatter.write_dl([(e.name, e.help) for e in entries])


@click.group(cls=CheckpointCLI, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="checkpoint-agents", prog_name="checkpoint")
def main() -> None:
    """Checkpoint — prove your agent works before your customers find out.

    \b
    Run your real agent, unmodified, against stateful copies of the services it
    calls (GitHub, Slack, Stripe, ...). Check what it actually did, not just what
    it said. Repeat until the pass rate means something, then ship or block.

    \b
    First time:
        checkpoint demo                                 # no setup, no API key
        checkpoint init --command "python my_agent.py"  # point it at yours
        checkpoint run                                  # try a scenario
        checkpoint gate                                 # the verdict CI reads
    """


__all__ = ["main"]
