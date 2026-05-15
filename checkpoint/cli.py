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
from .compare_diff import build_compare_diff as _build_compare_diff
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
@click.option("--docker-logs", is_flag=True, default=False, help="Stream harness container logs to stderr in real time (docker mode only).")
def run(scenario_path, harness, task, clone, runs, model, timeout, cwd, trace_out, tag, reuse_session, docker, harness_dir, docker_logs):
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

        # On Windows, shlex.split with posix=True treats backslashes as escape
        # chars, mangling Windows paths. posix=False preserves them as literals.
        harness_cmd = shlex.split(harness_cmd_str, posix=sys.platform != "win32") if harness_cmd_str else []

        results: list[RunResult] = []
        for i in range(scenario.runs):
            console.print(f"\n[bold]Run {i + 1}/{scenario.runs}[/bold]")
            if docker:
                from .docker.runner import docker_run_once
                hdir = Path(harness_dir or cwd or ".").resolve()
                r = docker_run_once(scenario, harness_cmd, hdir, cwd=cwd, judge_model=resolution.model, verbose=docker_logs)
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
            mark = "[green]PASS[/green]" if c.passed else "[red]FAIL[/red]"
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


@main.command("init")
@click.argument("target_dir", required=False, type=click.Path(file_okay=False), default=".")
@click.option(
    "--template",
    default="raw",
    show_default=True,
    type=click.Choice(["raw", "anthropic", "openai-agents", "langchain"], case_sensitive=False),
    help="Harness template to scaffold (raw=plain requests, anthropic=Claude SDK, openai-agents=OpenAI Agents SDK, langchain=LangChain).",
)
def init_cmd(target_dir, template):
    """CLI-04: scaffold a Checkpoint integration in TARGET_DIR (default cwd)."""
    from . import init as _init

    try:
        result = _init.scaffold(target_dir, template=template)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]init failed: {e}[/red]")
        sys.exit(1)

    if result.created:
        t = Table(box=box.SIMPLE_HEAD)
        t.add_column("", style="dim", width=2)
        t.add_column("File")
        for p in result.created:
            t.add_row("[green]+[/green]", p)
        for p in result.skipped:
            t.add_row("[yellow]=[/yellow]", f"{p} (already exists, kept)")
        console.print(t)
    else:
        console.print("[yellow]All scaffold files already exist; nothing to do.[/yellow]")

    console.print(Panel.fit(result.banner, border_style="green", title="checkpoint init"))


@main.command()
def doctor():
    """CLI-03: check environment readiness."""
    checks = _diag.run_checks()
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("", style="dim", width=2)
    t.add_column("Check")
    t.add_column("Detail", overflow="fold")
    for c in checks:
        mark = "[green]PASS[/green]" if c.ok else "[red]FAIL[/red]"
        t.add_row(mark, c.name, c.detail)
    console.print(t)
    failed = [c for c in checks if not c.ok]
    if failed:
        console.print()
        console.print("[bold red]Fix the following:[/bold red]")
        for c in failed:
            if c.fix:
                console.print(f"  [red]FAIL[/red] {c.name}: [yellow]{c.fix}[/yellow]")
            else:
                console.print(f"  [red]FAIL[/red] {c.name}")
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


def _suggest_reword(text: str) -> str | None:
    """Return a reword hint when a [D] criterion noun is recognisable but unmatched."""
    import re as _re
    from .checker import _RESOURCE_MAP
    t = text.lower()
    for noun, _twin, _key in _RESOURCE_MAP:
        if _re.search(r"\b" + _re.escape(noun) + r"\b", t):
            return (
                f'Try: "Exactly N {noun}s exist" / '
                f'"An {noun} titled \\"…\\" exists" / '
                f'"At least N {noun}s are <state>"'
            )
    return None


@scenario.command("generate")
@click.argument("description")
@click.option("--output", "-o", type=click.Path(dir_okay=False), default=None,
              help="Write to file instead of stdout.")
@click.option("--clone", default=None, help="Twin clone(s) to use (comma-sep).")
@click.option("--model", default="gpt-4o-mini", show_default=True,
              help="LLM model to use for generation.")
def scenario_generate(description, output, clone, model):
    """Generate a scenario .md from a prose description (uses LLM)."""
    import os
    import tempfile
    from .scenario_gen import generate as _gen

    content = _gen(description, clone=clone, model=model)

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp = f.name
    try:
        parse_file(Path(tmp))
    except Exception as e:
        console.print(f"[yellow]Warning: generated file may be malformed: {e}[/yellow]")
    finally:
        os.unlink(tmp)

    if output:
        Path(output).write_text(content, encoding="utf-8")
        console.print(f"[green]Wrote scenario to {output}[/green]")
    else:
        click.echo(content)


@scenario.command("coverage")
@click.argument("path", required=False, type=click.Path(exists=True), default=".")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit JSON instead of a table.")
def scenario_coverage(path, as_json):
    """Report Stage-1 pattern hit rate for [D] criteria under PATH."""
    from .checker import PATTERNS as _checker_patterns

    rows = []
    for md in sorted(Path(path).rglob("*.md")):
        try:
            scn = parse_file(md)
        except Exception:
            continue
        if not (scn.prompt or scn.criteria):
            continue
        for crit in scn.criteria:
            if crit.kind != "D":
                continue
            hit = any(pat.search(crit.text) for pat, _ in _checker_patterns)
            rows.append({
                "scenario": md.name,
                "criterion": crit.text,
                "stage1": hit,
            })

    if as_json:
        total = len(rows)
        hit_count = sum(1 for r in rows if r["stage1"])
        click.echo(json.dumps({
            "total_d": total,
            "stage1_hits": hit_count,
            "stage1_pct": round(100 * hit_count / total, 1) if total else 0,
            "rows": rows,
        }, indent=2))
        return

    if not rows:
        console.print(f"[yellow]No [D] criteria found under {path}[/yellow]")
        return

    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Scenario")
    t.add_column("Criterion", overflow="fold")
    t.add_column("Stage 1?", width=9)
    for r in rows:
        mark = "[green]PASS[/green]" if r["stage1"] else "[red]FAIL[/red]"
        t.add_row(r["scenario"], r["criterion"][:100], mark)
    console.print(t)

    total = len(rows)
    hits = sum(1 for r in rows if r["stage1"])
    pct = 100 * hits // total if total else 0
    color = "green" if pct >= 80 else ("yellow" if pct >= 50 else "red")
    console.print(
        f"\nStage-1 coverage: [{color}]{hits}/{total} ({pct}%)[/{color}] of [D] criteria"
    )


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
            mark = "[green]PASS[/green]" if c.get("passed") else "[red]FAIL[/red]"
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


# =============================================================================
# runs list / compare
# =============================================================================


@main.group("runs")
def runs_group():
    """List and compare past run records."""


@runs_group.command("list")
@click.option("--limit", "-n", default=20, show_default=True, help="Max rows to display.")
@click.option("--scenario", "-s", default=None, help="Filter by scenario name substring.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def runs_list(limit, scenario, as_json):
    """List recent run records from .checkpoint/cache/runs/."""
    rows = _load_recent_runs(limit, scenario_filter=scenario)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("[yellow]No run records found. Run `checkpoint run` first.[/yellow]")
        return
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Run ID", style="dim", width=14)
    t.add_column("Scenario", overflow="fold")
    t.add_column("Score", width=7)
    t.add_column("Criteria", width=9)
    t.add_column("Model", overflow="fold")
    t.add_column("Timestamp")
    for row in rows:
        sat = row.get("satisfaction", 0)
        color = "green" if sat == 100 else ("yellow" if sat >= 50 else "red")
        passed = sum(1 for c in (row.get("criteria") or []) if c.get("passed"))
        total = len(row.get("criteria") or [])
        ts = (row.get("env") or {}).get("timestamp", "?")
        t.add_row(
            row.get("run_id", "?")[:12],
            (row.get("scenario") or "?")[:50],
            f"[{color}]{sat:.0f}[/{color}]",
            f"{passed}/{total}",
            row.get("evaluator_model", "?"),
            ts,
        )
    console.print(t)


def _load_recent_runs(limit: int, scenario_filter: str | None = None) -> list[dict]:
    """Load up to `limit` run records sorted newest-first, optionally filtered by scenario name."""
    if not RUNS_DIR.exists():
        return []
    files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    pattern = scenario_filter.lower() if scenario_filter else None
    for f in files:
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if pattern and pattern not in (rec.get("scenario") or "").lower():
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


@main.command("compare")
@click.argument("run_id_a")
@click.argument("run_id_b")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON diff.")
def compare(run_id_a, run_id_b, as_json):
    """Compare two run records (score regression, criterion-level diff).

    RUN_ID_A is the baseline; RUN_ID_B is the candidate.
    """
    rec_a = _load_run_record(run_id_a)
    rec_b = _load_run_record(run_id_b)
    missing = []
    if rec_a is None:
        missing.append(run_id_a)
    if rec_b is None:
        missing.append(run_id_b)
    if missing:
        console.print(f"[red]Run records not found: {', '.join(missing)}[/red]")
        sys.exit(1)

    diff = _build_compare_diff(rec_a, rec_b)

    if as_json:
        click.echo(json.dumps(diff, indent=2))
        return

    _print_compare(diff, run_id_a, run_id_b, rec_a, rec_b)



def _print_compare(diff: dict, id_a: str, id_b: str, rec_a: dict, rec_b: dict) -> None:
    sat_a = diff["baseline_score"]
    sat_b = diff["candidate_score"]
    delta = diff["delta"]
    delta_str = f"+{delta}" if delta >= 0 else str(delta)
    delta_color = "green" if delta > 0 else ("red" if delta < 0 else "dim")

    header = (
        f"[bold]Baseline:[/bold]  {id_a[:12]}  [{_score_color(sat_a)}]{sat_a:.0f}/100[/{_score_color(sat_a)}]"
        f"  [dim]{rec_a.get('scenario', '?')}[/dim]\n"
        f"[bold]Candidate:[/bold] {id_b[:12]}  [{_score_color(sat_b)}]{sat_b:.0f}/100[/{_score_color(sat_b)}]"
        f"  [dim]{rec_b.get('scenario', '?')}[/dim]\n"
        f"[bold]Delta:[/bold]     [{delta_color}]{delta_str}[/{delta_color}]"
    )
    border = "green" if delta >= 0 else "red"
    console.print(Panel.fit(header, title="checkpoint compare", border_style=border))

    if diff["regressions"]:
        console.print("\n[bold red]Regressions (passed → failed)[/bold red]")
        for d in diff["regressions"]:
            console.print(f"  [red]FAIL[/red] {d['text'][:120]}")

    if diff["fixes"]:
        console.print("\n[bold green]Fixes (failed → passed)[/bold green]")
        for d in diff["fixes"]:
            console.print(f"  [green]PASS[/green] {d['text'][:120]}")

    if diff["added"]:
        console.print("\n[dim]New criteria (only in candidate)[/dim]")
        for d in diff["added"]:
            mark = "[green]PASS[/green]" if d["candidate_passed"] else "[red]FAIL[/red]"
            console.print(f"  {mark} {d['text'][:120]}")

    if diff["removed"]:
        console.print("\n[dim]Removed criteria (only in baseline)[/dim]")
        for d in diff["removed"]:
            mark = "[green]PASS[/green]" if d["baseline_passed"] else "[red]FAIL[/red]"
            console.print(f"  {mark} {d['text'][:120]}")

    if not diff["regressions"] and not diff["fixes"]:
        console.print("\n[dim]No criterion-level changes.[/dim]")


def _score_color(sat: float) -> str:
    if sat == 100:
        return "green"
    if sat >= 50:
        return "yellow"
    return "red"


# =============================================================================
# CI/CD helpers
# =============================================================================

@main.group("ci")
def ci_group():
    """CI/CD integration helpers."""


@ci_group.command("init")
@click.argument("target_dir", required=False, type=click.Path(file_okay=False), default=".")
@click.option("--pre-commit", "with_pre_commit", is_flag=True, default=False,
              help="Also write .pre-commit-config.yaml.")
def ci_init(target_dir, with_pre_commit):
    """Scaffold a GitHub Actions workflow into TARGET_DIR (default cwd)."""
    import shutil
    tpl_dir = Path(__file__).parent / "init_templates" / "ci"
    target = Path(target_dir)
    wf_dir = target / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    dest = wf_dir / "checkpoint.yml"
    if dest.exists():
        console.print(f"[yellow]=[/yellow] {dest} (already exists, kept)")
    else:
        shutil.copy(tpl_dir / "checkpoint.yml", dest)
        console.print(f"[green]+[/green] {dest}")
    if with_pre_commit:
        pc_dest = target / ".pre-commit-config.yaml"
        if pc_dest.exists():
            console.print(f"[yellow]=[/yellow] {pc_dest} (already exists, kept)")
        else:
            shutil.copy(tpl_dir / "pre-commit-config.yaml", pc_dest)
            console.print(f"[green]+[/green] {pc_dest}")
    console.print(Panel.fit(
        "Add OPENAI_API_KEY to GitHub Secrets, then push.\n"
        + ("Run `pre-commit install` to activate the hook." if with_pre_commit else ""),
        title="checkpoint ci init",
        border_style="green",
    ))


@main.command("badge")
@click.argument("run_id", required=False)
@click.option("--md", "as_md", is_flag=True, default=False,
              help="Emit Markdown `![…](…)` instead of raw URL.")
@click.option("--label", default="checkpoint", show_default=True,
              help="Left-hand label on the badge.")
def badge_cmd(run_id, as_md, label):
    """Generate a shields.io badge URL from a run record (defaults to last run)."""
    record = _load_run_record(run_id)
    if record is None:
        console.print("[red]No run record found. Run `checkpoint run` first.[/red]")
        sys.exit(1)
    sat = record.get("satisfaction", 0)
    color = "brightgreen" if sat == 100 else ("yellow" if sat >= 50 else "red")
    pct_encoded = f"{sat:.0f}%25"
    label_encoded = label.replace("-", "--").replace("_", "__")
    url = f"https://img.shields.io/badge/{label_encoded}-{pct_encoded}-{color}"
    if as_md:
        click.echo(f"![{label}]({url})")
    else:
        click.echo(url)


# =============================================================================
# Reporting
# =============================================================================

@main.command("report")
@click.argument("scenario_pattern", required=False, default="")
@click.option("--limit", "-n", default=50, show_default=True,
              help="Max runs to aggregate.")
@click.option("--json", "as_json", is_flag=True, default=False)
def report(scenario_pattern, limit, as_json):
    """Aggregate pass-rate trend and flaky criteria across recent runs.

    SCENARIO_PATTERN is a substring filter on scenario name (empty = all runs).
    """
    from .analytics import load_runs_for_scenario, compute_trend, detect_flaky

    runs = load_runs_for_scenario(scenario_pattern, RUNS_DIR, limit)
    if not runs:
        console.print("[yellow]No matching run records found.[/yellow]")
        sys.exit(0)

    trend = compute_trend(runs)
    flaky = detect_flaky(trend)

    if as_json:
        click.echo(json.dumps({**trend, "flaky_criteria": flaky}, indent=2))
        return

    console.print(Panel.fit(
        f"[bold]Scenario:[/bold] {scenario_pattern or '(all)'}\n"
        f"[bold]Runs:[/bold] {trend['run_count']}  "
        f"[bold]Avg:[/bold] {trend['avg_score']}/100  "
        f"[bold]Range:[/bold] {trend['min_score']}-{trend['max_score']}",
        title="checkpoint report",
        border_style="cyan",
    ))

    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Criterion", overflow="fold")
    t.add_column("Kind", width=4)
    t.add_column("Pass rate", width=10)
    t.add_column("Runs", width=6)
    t.add_column("Stable?", width=8)
    for text, s in sorted(trend["criteria"].items()):
        rate = s["pass_rate"]
        color = "green" if rate >= 0.8 else ("yellow" if rate >= 0.5 else "red")
        stable = "[yellow]FLAKY[/yellow]" if text in flaky else "[green]ok[/green]"
        t.add_row(
            text[:80], s["kind"],
            f"[{color}]{rate:.0%}[/{color}]",
            str(s["total"]), stable,
        )
    console.print(t)

    if flaky:
        console.print(f"\n[yellow]{len(flaky)} flaky criterion(a) detected.[/yellow]")


# =============================================================================
# Docker utilities
# =============================================================================

@main.group("docker")
def docker_group():
    """Docker harness utilities."""


@docker_group.command("build")
@click.argument("harness_dir", required=False,
                type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--tag", default=None,
              help="Image tag (default: checkpoint-harness:latest).")
def docker_build(harness_dir, tag):
    """Pre-build the harness Docker image without running a scenario."""
    from .docker.harness_image import build_harness_image
    tag = tag or "checkpoint-harness:latest"
    console.print(f"[dim]Building harness image from {harness_dir!r} -> {tag}[/dim]")
    try:
        build_harness_image(Path(harness_dir), tag=tag)
        console.print(f"[green]Built {tag}[/green]")
    except Exception as e:
        console.print(f"[red]Build failed: {e}[/red]")
        sys.exit(1)


@main.command("validate")
@click.argument("scenario_path", type=click.Path(exists=True))
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit structured JSON instead of a table.")
def validate(scenario_path, as_json):
    """Validate a scenario file: parse, check required sections, lint criteria patterns.

    Exits 0 if valid, 1 if any errors are found.
    """
    from .checker import PATTERNS as _checker_patterns

    path = Path(scenario_path)
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Parse
    try:
        scn = parse_file(path)
    except Exception as exc:
        errors.append(f"Parse error: {exc}")
        if as_json:
            click.echo(json.dumps({"valid": False, "errors": errors, "warnings": []}, indent=2))
        else:
            console.print(f"[red]Parse error: {exc}[/red]")
        sys.exit(1)

    # 2. Required sections
    if not scn.prompt:
        errors.append("Missing required section: ## Prompt (or ## Task)")
    if not scn.criteria:
        warnings.append("No success criteria found (## Success Criteria / ## Checks)")

    # 3. Criteria pattern coverage
    unhandled: list[str] = []
    for crit in scn.criteria:
        text = crit.text.strip()
        kind = crit.kind  # "D" or "P"
        if kind == "D":
            # PATTERNS is list[tuple[re.Pattern, handler]]; use .search() directly.
            matched = any(pat.search(text) for pat, _ in _checker_patterns)
            if not matched:
                unhandled.append(text)

    if unhandled:
        lines = []
        for t in unhandled:
            lines.append(f"  - {t[:120]}")
            hint = _suggest_reword(t)
            if hint:
                lines.append(f"    -> {hint}")
        warnings.append(
            f"{len(unhandled)} [D] criterion(a) have no deterministic pattern match "
            f"(will fall through to LLM stage 2):\n" + "\n".join(lines)
        )

    # 4. Clone validity
    known_clones = {
        "github", "slack", "stripe", "linear", "supabase", "discord", "google-workspace",
    }
    for clone_id in scn.clones:
        if clone_id not in known_clones:
            errors.append(f"Unknown clone: {clone_id!r} (known: {', '.join(sorted(known_clones))})")

    # 5. Config key spelling
    known_config_keys = {
        "clones", "seed", "seed-file", "seed_file", "seed_name", "runs",
        "timeout", "tags", "evaluator-model", "evaluator_model",
    }
    for key in scn.config:
        if key not in known_config_keys:
            warnings.append(f"Unknown config key: {key!r}")

    valid = len(errors) == 0

    if as_json:
        click.echo(json.dumps({
            "valid": valid,
            "scenario": str(path),
            "title": scn.title,
            "clones": list(scn.clones),
            "runs": scn.runs,
            "criteria_count": len(scn.criteria),
            "errors": errors,
            "warnings": warnings,
        }, indent=2))
    else:
        console.print(Panel.fit(
            f"[bold]{scn.title or path.name}[/bold]\n"
            f"[dim]clones:[/dim] {', '.join(scn.clones) or '(none)'}\n"
            f"[dim]criteria:[/dim] {len(scn.criteria)} "
            f"({sum(1 for c in scn.criteria if c.kind == 'D')} [D], "
            f"{sum(1 for c in scn.criteria if c.kind == 'P')} [P])\n"
            f"[dim]runs:[/dim] {scn.runs}",
            title=f"validate — {path.name}",
            border_style="cyan",
        ))
        if errors:
            for e in errors:
                console.print(f"[red]  Error:[/red] {e}")
        if warnings:
            for w in warnings:
                console.print(f"[yellow]  Warning:[/yellow] {w}")
        if valid:
            console.print("[green]  Scenario is valid.[/green]")
        else:
            console.print(f"[red]  {len(errors)} error(s) found.[/red]")

    sys.exit(0 if valid else 1)


@main.command("replay")
@click.argument("run_id", required=False)
@click.option("--clone", default=None, help="Filter trace to a specific clone.")
@click.option("--limit", "-n", default=50, show_default=True, help="Max trace events to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit raw JSON trace.")
def replay(run_id, clone, limit, as_json):
    """Replay the API trace from a past run to inspect what the agent did.

    Defaults to the most recent run. Useful for debugging failing criteria by
    seeing exactly which API calls were made and in what order.
    """
    record = _load_run_record(run_id)
    if record is None:
        if run_id:
            console.print(f"[red]No run record for id={run_id}[/red]")
        else:
            console.print("[red]No last-run pointer. Run `checkpoint run` first.[/red]")
        sys.exit(1)

    trace = record.get("trace") or []

    # Trace may be a flat list or {clone: [events]} dict (multi-clone runs).
    if isinstance(trace, dict):
        if clone:
            trace = trace.get(clone, [])
        else:
            # Flatten all clone traces in order (interleaved by index).
            merged: list[dict] = []
            for clone_id, events in trace.items():
                for ev in events:
                    merged.append({**ev, "_clone": clone_id})
            trace = merged
    elif clone:
        trace = [ev for ev in trace if ev.get("clone") == clone or ev.get("_clone") == clone]

    if as_json:
        click.echo(json.dumps(trace[:limit], indent=2, default=str))
        return

    rid = record.get("run_id", "?")
    sat = record.get("satisfaction", 0.0)
    color = "green" if sat == 100 else ("yellow" if sat >= 50 else "red")
    console.print(Panel.fit(
        f"[bold]Replay: run {rid[:12]}[/bold]\n"
        f"[dim]scenario:[/dim] {record.get('scenario', '?')}\n"
        f"[dim]score:[/dim] [{color}]{sat}/100[/{color}]\n"
        f"[dim]events:[/dim] {len(trace)} total (showing first {min(limit, len(trace))})",
        border_style=color,
        title="checkpoint replay",
    ))

    t = Table(box=box.SIMPLE_HEAD, show_lines=False)
    t.add_column("#", style="dim", width=4)
    t.add_column("Clone", width=16)
    t.add_column("Method", width=7)
    t.add_column("Path", overflow="fold")
    t.add_column("Status", width=6)

    for i, ev in enumerate(trace[:limit]):
        clone_id = ev.get("_clone") or ev.get("clone") or "—"
        method = ev.get("method") or ev.get("type") or "—"
        path = ev.get("path") or ev.get("url") or "—"
        status = str(ev.get("status") or ev.get("status_code") or "—")
        t.add_row(str(i + 1), clone_id, method, path[:80], status)

    console.print(t)


@main.command("serve")
@click.option("--port", default=4001, show_default=True, help="Port to listen on.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option(
    "--scenarios", "scenarios_dir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Directory to scan for .md scenario files.",
)
@click.option("--open/--no-open", "auto_open", default=False,
              help="Open the dashboard in the default browser.")
@click.option("--judge-model", default="gpt-4o-mini", show_default=True,
              help="Default judge model surfaced in the dashboard meta + new runs.")
def serve(port, host, scenarios_dir, auto_open, judge_model):
    """Start the checkpoint web dashboard."""
    import logging
    import uvicorn
    import webbrowser
    from .dashboard.app import create_app
    from .clone_manager import DEFAULT_REGISTRY

    # Wire stdlib logging once so middleware + watcher logs appear under
    # uvicorn's color-aware handler.
    logging.basicConfig(
        level=os.environ.get("CHECKPOINT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    scenarios_path = Path(scenarios_dir).resolve()
    app = create_app(
        runs_dir=RUNS_DIR,
        scenarios_dir=scenarios_path,
        clone_registry_path=DEFAULT_REGISTRY,
        project_dir=Path.cwd(),
        judge_model_default=judge_model,
    )
    url = f"http://{host}:{port}"
    console.print(Panel.fit(
        f"[bold]Dashboard:[/bold]  {url}\n"
        f"[bold]API docs:[/bold]   {url}/api/docs\n"
        f"[dim]Runs dir:[/dim]    {RUNS_DIR.resolve()}\n"
        f"[dim]Scenarios:[/dim]   {scenarios_path}\n\n"
        f"Press Ctrl-C to stop.",
        title="checkpoint serve",
        border_style="green",
    ))
    if auto_open:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
