"""Starting Checkpoint has to stay cheap, or nobody reaches for it.

Two budgets. ``checkpoint --help`` must not pay for the dashboard, the MCP
server or an LLM SDK just to print fifteen lines — the command table names each
command's module and imports it only when it runs, and that only holds while
nothing drags a heavy import up to the top of the package. And a real run has a
wall-clock budget, because a test tool slower than the thing it tests gets run
once.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SCENARIO = REPO_ROOT / "checkpoint" / "demo" / "smoke-scenario.md"
DEMO_AGENT = REPO_ROOT / "checkpoint" / "demo" / "harness_fake.py"

#: Modules that cost real time to import and that no `--help` needs.
HEAVY = ("fastapi", "uvicorn", "starlette", "openai", "mcp", "httpx",
         "checkpoint.dashboard", "checkpoint.engine", "checkpoint.twins",
         "checkpoint.gate", "checkpoint.redteam", "checkpoint.llm")

#: The spec is under 5s on a dev box; the slack absorbs CI and slow disks.
COLD_START_BUDGET_SECONDS = 8.0

_PROBE = f"""
import sys
from click.testing import CliRunner
from checkpoint.cli import main
result = CliRunner().invoke(main, ["--help"])
assert result.exit_code == 0, result.output
print(",".join(m for m in {HEAVY!r} if m in sys.modules))
"""


def test_help_does_not_import_what_it_does_not_print():
    """A heavy import at package scope makes every command pay for one command."""
    probe = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                           text=True, cwd=REPO_ROOT, timeout=120)
    assert probe.returncode == 0, probe.stderr
    loaded = [m for m in probe.stdout.strip().split(",") if m]
    assert loaded == [], f"`checkpoint --help` imported {loaded}"


def test_help_lists_every_command_in_the_table():
    """The sections are written by hand, so a command can be added and not shown."""
    from click.testing import CliRunner

    from checkpoint.cli import SECTIONS, main

    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    for _title, entries in SECTIONS:
        for entry in entries:
            assert entry.name in result.output, f"{entry.name} is missing from --help"


@pytest.mark.skipif(not DEMO_SCENARIO.is_file() or not DEMO_AGENT.is_file(),
                    reason="demo assets missing")
def test_a_scored_run_completes_within_the_cold_start_budget():
    """Time a fresh process all the way through scoring a deterministic scenario."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    command = [sys.executable, "-m", "checkpoint.cli", "run", str(DEMO_SCENARIO),
               "--command", f"{sys.executable} {DEMO_AGENT}", "--json"]

    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=REPO_ROOT,
                          timeout=COLD_START_BUDGET_SECONDS + 60)
    elapsed = time.monotonic() - started

    # The scenario has only [D] criteria, so this never calls a judge model.
    assert proc.returncode == 0, f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}"
    payload = json.loads(proc.stdout)
    assert payload["passed"] == payload["runs"] == 1
    assert elapsed < COLD_START_BUDGET_SECONDS, (
        f"cold start took {elapsed:.2f}s (budget {COLD_START_BUDGET_SECONDS}s)")
