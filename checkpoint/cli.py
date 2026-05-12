"""checkpoint CLI."""
from __future__ import annotations

import json
import os
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
from .config import (
    CheckpointConfig,
    HarnessConfig,
    load_checkpoint_config,
    load_harness_config,
    resolve_evaluator_model,
    matches_tag,
)

console = Console()


@click.group()
def main():
    """checkpoint: agent testing against stateful SaaS twins."""


@main.command()
@click.argument("scenario_path", required=False, type=click.Path(exists=True))
@click.option("--harness", default=None, help="Shell command, harness file, harness dir, or harness.json. Falls back to .checkpoint.json/harness.json.")
@click.option("--task", default=None, help="Inline task (overrides scenario prompt; works without a scenario file).")
@click.option("--clone", default=None, help="Override scenario clones (comma-separated).")
@click.option("--runs", type=int, default=None, help="Override number of runs.")
@click.option("--model", default=None, help="Evaluator model. Overrides scenario/config/env.")
@click.option("--timeout", type=int, default=None, help="Override harness timeout (seconds).")
@click.option("--cwd", type=click.Path(exists=True, file_okay=False), default=None, help="Working dir for the harness.")
@click.option("--trace-out", type=click.Path(dir_okay=False), default=None, help="Save all-runs trace+state JSON to file.")
@click.option("--tag", default=None, help="Filter scenarios in a directory by `tags:` config (comma-sep).")
@click.option("--reuse-session", is_flag=True, default=False, help="(stub) Reuse hosted session; no-op in v1.")
@click.option("--docker/--no-docker", default=False, help="Run the harness inside Docker with the TLS sidecar.")
@click.option("--harness-dir", type=click.Path(exists=True, file_okay=False), default=None, help="Harness directory containing Dockerfile (docker mode).")
def run(scenario_path, harness, task, clone, runs, model, timeout, cwd, trace_out, tag, reuse_session, docker, harness_dir):
    """Run scenario(s) against the agent harness.

    SCENARIO_PATH may be a single .md file or a directory of scenarios.
    """
    # --- Load .checkpoint.json + harness.json (auto-discovery) ---
    ckpt_cfg = load_checkpoint_config()
    harness_cfg = load_harness_config(harness)

    if reuse_session:
        # SCOPE §7: hosted session reuse — local v1 has no hosted sessions.
        console.print("[dim]--reuse-session: hosted sessions unavailable in local v1; ignoring.[/dim]")

    # --- Resolve harness command ---
    harness_cmd_str = harness
    if not harness_cmd_str:
        # Try harness.json first, then .checkpoint.json.
        if harness_cfg.path:
            harness_cmd_str = f"{sys.executable} {harness_cfg.path}"
        elif ckpt_cfg.harness_path:
            harness_cmd_str = f"{sys.executable} {ckpt_cfg.harness_path}"

    if harness_cmd_str:
        harness_cmd_str = _normalize_harness_arg(harness_cmd_str)

    if not harness_cmd_str and not docker:
        console.print("[red]No harness specified. Pass --harness, set `harness.path` in .checkpoint.json, or add a harness.json.[/red]")
        sys.exit(2)

    # --- Determine scenario file list ---
    resolved = _resolve_scenario_files(scenario_path, task)
    if resolved is None:
        console.print("[red]Provide a scenario file/dir or --task[/red]")
        sys.exit(2)
    scenario_files, is_directory = resolved

    # --- Iterate scenarios with --tag filter ---
    any_failed = False
    any_run = False
    all_run_dumps: list[dict] = []
    for scn_path in scenario_files:
        scenario = parse_file(scn_path) if scn_path else Scenario()
        if task:
            scenario.prompt = task

        # SCN-10: --tag filter (only applies when iterating a directory).
        if tag and is_directory:
            if not matches_tag(scenario.config.get("tags"), tag):
                console.print(f"[dim]skip (tag mismatch): {scn_path}[/dim]")
                continue

        # --- Merge config defaults from .checkpoint.json ---
        if clone:
            scenario.config["clones"] = clone
        elif not scenario.clones and ckpt_cfg.clones:
            scenario.config["clones"] = ",".join(ckpt_cfg.clones)
        if not scenario.clones:
            scenario.config["clones"] = "github"

        # Default named seeds from .checkpoint.json apply only when the
        # scenario gives the runtime no other guidance:
        #   - no `seed:` / `seed-file:` already set, AND
        #   - no `## Setup` prose (which would otherwise derive its own seed).
        # This keeps scenarios with their own seeding story (`fresh workspace`,
        # `incident-active`, etc.) reproducible regardless of repo-root config.
        has_explicit_seed = (
            "seed" in scenario.config
            or "seed_name" in scenario.config
            or "seed-file" in scenario.config
            or "seed_file" in scenario.config
        )
        has_setup_prose = bool((scenario.setup or "").strip())
        if not has_explicit_seed and not has_setup_prose and ckpt_cfg.seeds:
            # Apply only the seeds for clones the scenario actually uses.
            applicable = {k: v for k, v in ckpt_cfg.seeds.items() if k in scenario.clones}
            if applicable:
                scenario.config["seed"] = ", ".join(f"{k}={v}" for k, v in applicable.items())

        if runs is not None:
            scenario.config["runs"] = str(runs)
        if timeout is not None:
            scenario.config["timeout"] = str(timeout)

        if not scenario.prompt:
            console.print(f"[red]Scenario {scn_path} has no Prompt and no --task — skipping[/red]")
            continue

        # --- Resolve evaluator model with documented precedence ---
        resolution = resolve_evaluator_model(
            flag_value=model,
            scenario_value=scenario.config.get("evaluator-model") or scenario.config.get("evaluator_model"),
            config_value=ckpt_cfg.evaluator_model,
            env_value=os.environ.get("ARCHAL_MODEL"),
        )

        console.print(Panel.fit(
            f"[bold]{scenario.title or 'Untitled scenario'}[/bold]\n"
            f"[dim]clone:[/dim] {', '.join(scenario.clones)}\n"
            f"[dim]runs:[/dim]  {scenario.runs}\n"
            f"[dim]judge:[/dim] {resolution.model} [dim]({resolution.source})[/dim]",
            title=f"checkpoint run — {Path(scn_path).name if scn_path else 'inline'}",
            border_style="cyan",
        ))

        if scenario.prompt:
            preview = scenario.prompt[:240]
            suffix = "…" if len(scenario.prompt) > 240 else ""
            console.print(f"[dim]Task:[/dim] {preview}{suffix}")

        harness_cmd = shlex.split(harness_cmd_str) if harness_cmd_str else []

        results: list[RunResult] = []
        for i in range(scenario.runs):
            console.print(f"\n[bold]Run {i + 1}/{scenario.runs}[/bold]")
            if docker:
                from .docker.runner import docker_run_once
                hdir = Path(harness_dir or cwd or ".").resolve()
                r = docker_run_once(scenario, harness_cmd, hdir, cwd=cwd, judge_model=resolution.model)
            else:
                r = run_once(scenario, harness_cmd, cwd=cwd, judge_model=resolution.model)
            results.append(r)
            _print_run(r)

        if scenario.runs > 1:
            _print_summary(results)

        for r in results:
            all_run_dumps.append({
                **_dump(r),
                "scenario_path": str(scn_path) if scn_path else None,
                "evaluator_model": resolution.model,
                "evaluator_model_source": resolution.source,
            })

        any_run = True
        if not all(r.complete and r.score == 100.0 for r in results):
            any_failed = True

    if trace_out:
        Path(trace_out).write_text(json.dumps(all_run_dumps, indent=2))
        console.print(f"[dim]Trace written to {trace_out}[/dim]")

    if not any_run:
        console.print("[yellow]No scenarios matched the filter.[/yellow]")
        sys.exit(0)
    if any_failed:
        sys.exit(1)


def _normalize_harness_arg(harness_str: str) -> str:
    """Turn `--harness` into a runnable command string.

    - If the path is a directory containing `harness.json`, expand it to
      `python <path-from-harness.json>` resolved relative to the dir.
    - If the path is a directory without harness.json, return as-is (caller errors).
    - If the path ends in `.py`, prefix with the active python.
    - Otherwise treat as a verbatim command string.
    """
    p = Path(harness_str)
    if p.is_dir():
        manifest = p / "harness.json"
        if manifest.is_file():
            try:
                raw = json.loads(manifest.read_text())
                target = raw.get("path")
                if isinstance(target, str):
                    resolved = (p / target).resolve()
                    return f"{sys.executable} {resolved}"
            except (json.JSONDecodeError, OSError):
                pass
        return harness_str
    if p.is_file() and p.suffix == ".py":
        return f"{sys.executable} {p.resolve()}"
    return harness_str


def _resolve_scenario_files(scenario_path: str | None, task: str | None) -> tuple[list[Path | None], bool] | None:
    """Return (scenario files, is_directory).

    A `None` entry in the list means "inline-task only, no file".
    Top-level `None` means "user supplied neither a scenario nor a task".
    """
    if not scenario_path:
        if task:
            return [None], False
        return None
    p = Path(scenario_path)
    if p.is_dir():
        files = sorted(p.glob("*.md"))
        return list(files), True
    return [p], False


def _dump(r: RunResult) -> dict:
    out = {
        "final_answer": r.final_answer,
        "exit_code": r.exit_code,
        "error": r.error,
        "score": r.score,
        "trace": r.trace,
        "state": r.state,
        "criteria": [c.__dict__ for c in r.criteria],
    }
    # DockerRunResult superset — preserve metrics + agent_trace if present.
    metrics = getattr(r, "metrics", None)
    agent_trace = getattr(r, "agent_trace", None)
    if metrics is not None:
        out["metrics"] = metrics
    if agent_trace is not None:
        out["agent_trace"] = agent_trace
    return out


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

    metrics = getattr(r, "metrics", None)
    if metrics:
        keys = ", ".join(sorted(k for k in metrics.keys()))
        console.print(f"[dim]metrics:[/dim] {keys}")


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
