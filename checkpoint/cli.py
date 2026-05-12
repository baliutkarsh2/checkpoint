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
from .failure_analyzer import analyze as analyze_failures
from .run_record import build_record, write_record, load_last_run, RUNS_DIR
from . import diagnostics as _diag

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

        # EV-05 + EV-06: per-run failure analysis + persisted run record.
        for r in results:
            _persist_run_record(
                r,
                scenario_name=scenario.title or (Path(scn_path).name if scn_path else "<inline>"),
                scenario_path=str(scn_path) if scn_path else None,
                evaluator_model=resolution.model,
                evaluator_model_source=resolution.source,
                task=scenario.prompt,
            )

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


def _persist_run_record(
    r: RunResult,
    *,
    scenario_name: str,
    scenario_path: str | None,
    evaluator_model: str,
    evaluator_model_source: str,
    task: str,
) -> None:
    """Build, optionally enrich with failure analysis, write to disk.

    Best-effort: never raise to the user — the score has already been printed.
    """
    failure_analysis: dict[str, str] = {}
    failed_texts = [c.text for c in r.criteria if not c.passed] if r.criteria else []
    if r.complete and failed_texts:
        try:
            failure_analysis = analyze_failures(
                failed_texts,
                task=task,
                final_answer=r.final_answer,
                trace=r.trace,
                state=r.state,
                model=evaluator_model,
            )
        except Exception as e:
            console.print(f"[dim]failure-analysis skipped: {e}[/dim]")
            failure_analysis = {}

    record = build_record(
        scenario_name=scenario_name,
        scenario_path=scenario_path,
        satisfaction=r.score,
        criteria=r.criteria,
        evaluator_model=evaluator_model,
        evaluator_model_source=evaluator_model_source,
        final_answer=r.final_answer,
        trace=r.trace,
        state=r.state,
        error=r.error,
        exit_code=r.exit_code,
        metrics=getattr(r, "metrics", None),
        agent_trace=getattr(r, "agent_trace", None),
        failure_analysis=failure_analysis or None,
    )
    try:
        path = write_record(record)
        console.print(f"[dim]Run record: {path}[/dim]")
    except Exception as e:
        console.print(f"[dim]Run record write failed: {e}[/dim]")


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


# =============================================================================
# Phase 7: long-tail CLI surface
# =============================================================================


@main.command()
def doctor():
    """CLI-03: check environment readiness."""
    checks = _diag.run_checks()
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("", style="dim", width=2)
    t.add_column("Check")
    t.add_column("Detail", overflow="fold")
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        t.add_row(mark, c.name, c.detail)
    console.print(t)
    failed = [c for c in checks if not c.ok]
    if failed:
        console.print()
        console.print("[bold red]Fix the following:[/bold red]")
        for c in failed:
            if c.fix:
                console.print(f"  [red]✗[/red] {c.name}: [yellow]{c.fix}[/yellow]")
            else:
                console.print(f"  [red]✗[/red] {c.name}")
        sys.exit(1)
    console.print("[green]All checks passed.[/green]")
    sys.exit(0)


@main.group()
def scenario():
    """Manage local scenario files."""


@scenario.command("list")
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON instead of a table.")
def scenario_list(path, as_json):
    """CLI-05: enumerate `.md` scenarios under PATH (default cwd)."""
    rows = _enumerate_scenarios(Path(path))
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print(f"[yellow]No scenarios found under {path}[/yellow]")
        return
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Path", overflow="fold")
    t.add_column("Title", overflow="fold")
    t.add_column("Tags")
    t.add_column("Clones")
    for r in rows:
        t.add_row(r["path"], r["title"], r["tags"] or "-", r["clones"] or "-")
    console.print(t)


def _enumerate_scenarios(root: Path) -> list[dict]:
    """Walk `root` for `.md` files, parse each, return summary rows.

    To distinguish scenario files from arbitrary README markdown we require
    at least one of: ``## Prompt`` / ``## Task``, ``## Success Criteria`` /
    ``## Checks``, or ``## Config``. A plain title alone is not enough.
    """
    rows: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        try:
            scn = parse_file(p)
        except Exception:
            continue
        has_section = bool(scn.prompt or scn.criteria or scn.config or scn.setup)
        if not has_section:
            continue
        rows.append({
            "path": str(p),
            "title": scn.title or "(untitled)",
            "tags": scn.config.get("tags", ""),
            "clones": ", ".join(scn.clones),
        })
    return rows


@main.group()
def traces():
    """Inspect persisted run records."""


@traces.command("detail")
@click.argument("run_id", required=False)
def traces_detail(run_id):
    """CLI-06: print the run record. Defaults to last-run."""
    record = _load_run_record(run_id)
    if record is None:
        if run_id:
            console.print(f"[red]No run record for id={run_id}[/red]")
        else:
            console.print("[red]No last-run pointer. Run `checkpoint run` first.[/red]")
        sys.exit(1)
    _print_run_record(record)


@traces.command("export")
@click.argument("run_id", required=False)
@click.option("--output", "-o", required=True, type=click.Path(dir_okay=False))
def traces_export(run_id, output):
    """CLI-06: write a run record JSON to disk."""
    record = _load_run_record(run_id)
    if record is None:
        if run_id:
            console.print(f"[red]No run record for id={run_id}[/red]")
        else:
            console.print("[red]No last-run pointer.[/red]")
        sys.exit(1)
    Path(output).write_text(json.dumps(record, indent=2, default=str))
    console.print(f"[green]Wrote run record to {output}[/green]")


def _load_run_record(run_id: str | None) -> dict | None:
    """Resolve a run record by id or fall back to last-run."""
    if not run_id:
        return load_last_run()
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _print_run_record(record: dict) -> None:
    rid = record.get("run_id", "?")
    sat = record.get("satisfaction", 0.0)
    color = "green" if sat == 100 else ("yellow" if sat >= 50 else "red")
    header = (
        f"[bold]Run {rid}[/bold] — {record.get('scenario', '?')}\n"
        f"[dim]model:[/dim] {record.get('evaluator_model', '?')} "
        f"[dim]({record.get('evaluator_model_source', '?')})[/dim]\n"
        f"[dim]score:[/dim] [{color}]{sat}/100[/{color}]"
    )
    console.print(Panel.fit(header, border_style=color))

    criteria = record.get("criteria") or []
    if criteria:
        t = Table(box=box.SIMPLE_HEAD)
        t.add_column("", style="dim", width=2)
        t.add_column("Kind", width=4)
        t.add_column("Criterion", overflow="fold")
        t.add_column("Eval", style="dim", width=14)
        t.add_column("Reasoning", overflow="fold")
        for c in criteria:
            mark = "[green]✓[/green]" if c.get("passed") else "[red]✗[/red]"
            t.add_row(
                mark,
                f"[{c.get('kind', '?')}]",
                c.get("text", "")[:200],
                c.get("evaluator", ""),
                (c.get("reasoning") or "")[:300],
            )
        console.print(t)

    fa = record.get("failure_analysis") or {}
    if fa:
        console.print("\n[bold]Failure analysis[/bold]")
        for crit, why in fa.items():
            console.print(f"  [dim]•[/dim] [yellow]{crit[:100]}[/yellow]")
            console.print(f"    {why}")

    state = record.get("state") or {}
    if state:
        console.print("\n[bold]Twin state[/bold]")
        # State may be flat (single clone) or {clone: state} (multi-clone).
        first = next(iter(state.values()), None)
        if isinstance(first, dict) and all(isinstance(v, dict) for v in state.values()):
            for clone, s in state.items():
                console.print(f"  [dim]{clone}:[/dim] {sorted(s.keys())[:10]}")
        else:
            console.print(f"  keys: {sorted(state.keys())[:20]}")


# =============================================================================
# Phase 7 / Task 2: clone start / inspect / stop
# =============================================================================


@main.group()
def clone():
    """Manage long-lived local twin sessions."""


@clone.command("start")
@click.argument("clone_id")
def clone_start(clone_id):
    """CLI-07: start a long-lived twin session (github/slack/stripe)."""
    from . import clone_manager
    try:
        entry = clone_manager.start(clone_id)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]clone start {clone_id}: {e}[/red]")
        sys.exit(1)
    console.print(Panel.fit(
        f"[bold]Clone {clone_id} started[/bold]\n"
        f"[dim]URL:[/dim]      {entry['url']}\n"
        f"[dim]MCP URL:[/dim]  {entry['mcp_url']}\n"
        f"[dim]Token:[/dim]    {entry['token']}\n"
        f"[dim]PID:[/dim]      {entry['pid']}",
        border_style="green",
    ))


@clone.command("inspect")
@click.argument("clone_id")
def clone_inspect(clone_id):
    """CLI-07: show running clone metadata + state/request counts."""
    from . import clone_manager
    info = clone_manager.inspect(clone_id)
    if info is None:
        console.print(f"[yellow]No registered clone for {clone_id!r}.[/yellow]")
        sys.exit(1)
    if not info.get("alive"):
        console.print(f"[yellow]Clone {clone_id!r} is registered but the "
                      f"process is gone. Registry purged.[/yellow]")
        sys.exit(1)
    body = (
        f"[bold]Clone {clone_id}[/bold] (alive)\n"
        f"[dim]URL:[/dim]            {info['url']}\n"
        f"[dim]MCP URL:[/dim]        {info['mcp_url']}\n"
        f"[dim]PID:[/dim]            {info['pid']}\n"
        f"[dim]Started:[/dim]        {info.get('started_at', '?')}\n"
        f"[dim]Request count:[/dim]  {info.get('request_count', 0)}\n"
        f"[dim]State size:[/dim]     {info.get('state_size', 0)} bytes\n"
        f"[dim]State keys:[/dim]     {info.get('state_keys', [])}"
    )
    console.print(Panel.fit(body, border_style="cyan"))


@clone.command("stop")
@click.argument("clone_id")
def clone_stop(clone_id):
    """CLI-07: stop a running twin session."""
    from . import clone_manager
    was_running = clone_manager.stop(clone_id)
    if was_running:
        console.print(f"[green]Stopped clone {clone_id}.[/green]")
    else:
        console.print(f"[yellow]Clone {clone_id!r} was not running "
                      f"(registry cleared).[/yellow]")


if __name__ == "__main__":
    main()
