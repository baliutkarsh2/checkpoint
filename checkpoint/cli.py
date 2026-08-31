"""checkpoint CLI."""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
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
from .telemetry import build_telemetry_report
from . import diagnostics as _diag

console = Console()


@click.group()
@click.version_option(package_name="checkpoint-agents", prog_name="checkpoint")
def main():
    """checkpoint: the release gate for AI agents — test against stateful SaaS twins."""


@main.command("demo")
@click.option("--dashboard", is_flag=True, default=False,
              help="After scoring, open the result in the web dashboard.")
@click.pass_context
def demo(ctx, dashboard):
    """Run a bundled scenario against a bundled agent — no Docker, no API key.

    The fastest "does Checkpoint work?" path: a tiny standard-library agent
    creates an issue on a local GitHub twin, and a deterministic [D]-only
    scenario is scored by the catalog — the LLM judge never runs, so nothing
    leaves your machine and no API key is needed. Wire up your own agent next
    with `checkpoint init --command "python my_agent.py"`.
    """
    demo_dir = Path(__file__).parent / "demo"
    scenario = str(demo_dir / "smoke-scenario.md")
    harness_py = demo_dir / "harness_fake.py"
    # Build a command the harness parser can re-split. It uses shlex with
    # posix=False on Windows (quotes are NOT stripped there), so quote only on
    # posix and pass raw tokens on Windows.
    if sys.platform == "win32":
        command = f"{sys.executable} {harness_py}"
    else:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(harness_py))}"
    click.echo("Running the Checkpoint demo — deterministic, offline, no API key.\n")
    ctx.invoke(
        run,
        scenario_path=scenario,
        inline_command=command,
        docker=False,
        no_failure_analysis=True,
    )
    if dashboard:
        ctx.invoke(serve)


@main.command()
@click.argument("scenario_path", required=False, type=click.Path(exists=True))
@click.option("--harness", default=None, help="Shell command, harness file, harness dir, or harness.json. Falls back to .checkpoint.json/harness.json.")
@click.option("--command", "inline_command", default=None,
              help="Inline command for your existing agent (e.g. `python my_agent.py`). "
                   "Checkpoint runs it for every scenario and injects the task via env "
                   "(CHECKPOINT_TASK by default). No harness.py file needed.")
@click.option("--task-via", type=click.Choice(["env", "arg", "stdin", "none"]),
              default=None, help="How to give your agent the task. Default `env`.")
@click.option("--task-env", default=None,
              help="Env var to set with the task when --task-via=env. Default CHECKPOINT_TASK.")
@click.option("--task-arg", default=None,
              help="Arg flag to use when --task-via=arg (e.g. `--prompt`). Task value follows.")
@click.option("--task", default=None, help="Inline task (overrides scenario prompt; works without a scenario file).")
@click.option("--clone", default=None, help="Override scenario clones (comma-separated).")
@click.option("-n", "--runs", type=int, default=None, help="Override number of runs.")
@click.option("--model", default=None, help="Evaluator (judge) model. Overrides scenario/config/env.")
@click.option("--timeout", type=int, default=None, help="Override harness timeout (seconds).")
@click.option("--cwd", type=click.Path(exists=True, file_okay=False), default=None, help="Working dir for the harness.")
@click.option("--trace-out", type=click.Path(dir_okay=False), default=None, help="Save all-runs trace+state JSON to file.")
@click.option("--tag", default=None, help="Filter scenarios in a directory by `tags:` config (comma-sep).")
@click.option("--reuse-session", is_flag=True, default=False, help="(stub) Reuse hosted session; no-op in v1.")
@click.option("--docker/--no-docker", default=True, show_default=True,
              help="Run the harness inside Docker with the TLS sidecar so real SDKs (PyGithub, supabase-py, ...) hit production URLs and get transparently routed to twins. Pass --no-docker for fast in-process runs (subprocess mode); useful for unit-test-style scenarios.")
@click.option("--harness-dir", type=click.Path(exists=True, file_okay=False), default=None, help="Harness directory containing Dockerfile (docker mode).")
@click.option("--docker-logs", is_flag=True, default=False, help="Stream harness container logs to stderr in real time (docker mode only).")
@click.option("--pass-threshold", type=int, default=None,
              help="Exit 1 if any scenario's avg satisfaction score falls below this (0-100). CI-friendly.")
@click.option("-o", "--output", "output_format", type=click.Choice(["table", "json"]), default="table",
              help="Output format. `json` emits a single machine-readable summary and suppresses table rendering.")
@click.option("-q", "--quiet", is_flag=True, default=False,
              help="Suppress per-run banners and panels; print only final summary (and JSON if -o json).")
@click.option("--rate-limit", type=int, default=None,
              help="Cap requests per twin (clones that support it return 429 after the limit; only github currently enforces).")
@click.option("--read-only", is_flag=True, default=False,
              help="Snapshot twin state pre-run, fail the run if state changed (no agent writes allowed).")
@click.option("--no-failure-analysis", is_flag=True, default=False,
              help="Skip the LLM-driven failure_analysis step (saves an LLM call per failed run).")
@click.option("--seed-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Override scenario seed-file (single-clone or `clone=path` comma-separated for multi-clone).")
@click.option("--setup-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Override scenario `## Setup` prose with the contents of this file.")
@click.option("--keep-state", is_flag=True, default=False,
              help="Don't reseed twin(s) before this run — keep state from a previous run.")
@click.option("--fresh-seed", is_flag=True, default=False,
              help="Force re-applying seed even if --keep-state was set previously (default behavior).")
def run(scenario_path, harness, inline_command, task_via, task_env, task_arg,
        task, clone, runs, model, timeout, cwd, trace_out, tag, reuse_session,
        docker, harness_dir, docker_logs, pass_threshold, output_format, quiet, rate_limit,
        read_only, no_failure_analysis, seed_file, setup_file, keep_state, fresh_seed):
    """Run scenario(s) against the agent harness.

    SCENARIO_PATH may be a single .md file or a directory of scenarios.
    """
    # --- Load .checkpoint.json + harness.json (auto-discovery) ---
    ckpt_cfg = load_checkpoint_config()
    harness_cfg = load_harness_config(harness)

    # --- User config fallbacks (~/.checkpoint/config.json) ---
    # Precedence: explicit flag > scenario > user config > built-in default.
    from .user_config import UserConfig
    _ucfg = UserConfig.load()
    _ucfg_runs = _ucfg.get("defaults.runs")
    if pass_threshold is None:
        _upt = _ucfg.get("defaults.pass_threshold")
        if isinstance(_upt, int):
            pass_threshold = _upt
    # Machine-local provider keys, only if the env var isn't already set.
    for _key, _envvar in (
        ("engine.openai_api_key", "OPENAI_API_KEY"),
        ("engine.anthropic_api_key", "ANTHROPIC_API_KEY"),
        ("engine.gemini_api_key", "GEMINI_API_KEY"),
    ):
        if not os.environ.get(_envvar):
            _v = _ucfg.get(_key)  # resolves `env:NAME` indirection
            if isinstance(_v, str) and _v:
                os.environ[_envvar] = _v
    _agent_model = _ucfg.get("defaults.agent_model")

    if reuse_session:
        # SCOPE §7: hosted session reuse — local v1 has no hosted sessions.
        console.print("[dim]--reuse-session: hosted sessions unavailable in local v1; ignoring.[/dim]")

    # --- Resolve harness command ---
    # Priority: --command (inline, zero-code) > --harness > harness.json > .checkpoint.json
    extra_env: dict[str, str] = {}
    if isinstance(_agent_model, str) and _agent_model:
        extra_env["CHECKPOINT_AGENT_MODEL"] = _agent_model
    if inline_command:
        # Zero-code path: pass the user's command verbatim. The runner already
        # shlex-splits it with the right posix flag for the platform. Task
        # injection happens via the sentinel env vars below.
        harness_cmd_str = inline_command
        chosen_via = task_via or "env"
        if chosen_via == "env":
            # Default — runner injects CHECKPOINT_TASK as env var (already does).
            if task_env and task_env != "CHECKPOINT_TASK":
                extra_env["CHECKPOINT_TASK_ENV"] = task_env
        elif chosen_via == "arg":
            extra_env["CHECKPOINT_TASK_VIA"] = "arg"
            if task_arg:
                extra_env["CHECKPOINT_TASK_ARG"] = task_arg
        elif chosen_via == "stdin":
            extra_env["CHECKPOINT_TASK_VIA"] = "stdin"
        elif chosen_via == "none":
            extra_env["CHECKPOINT_TASK_VIA"] = "none"
    else:
        harness_cmd_str = harness
        if not harness_cmd_str:
            # Try a v2 declarative harness.json first (command-based, zero
            # user code). Fall back to legacy {"path": "..."} or to
            # .checkpoint.json's harness.path.
            v2_spec = _maybe_load_v2_harness(harness_cfg.source_path)
            if v2_spec is not None:
                harness_cmd_str = " ".join(v2_spec.argv)
                if v2_spec.task_via != "env":
                    extra_env["CHECKPOINT_TASK_VIA"] = v2_spec.task_via
                if v2_spec.task_arg and v2_spec.task_via == "arg":
                    extra_env["CHECKPOINT_TASK_ARG"] = v2_spec.task_arg
                for k, v in v2_spec.env.items():
                    extra_env.setdefault(k, v)
            elif harness_cfg.path:
                harness_cmd_str = f"{sys.executable} {harness_cfg.path}"
            elif ckpt_cfg.harness_path:
                harness_cmd_str = f"{sys.executable} {ckpt_cfg.harness_path}"

    # Push extra env into the process so the runner picks it up.
    for k, v in extra_env.items():
        os.environ[k] = v

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

    # Surface runtime knobs to the runner via env vars (kept out of run_once
    # signatures to avoid a 7-arg function and to remain backwards-compatible).
    if rate_limit is not None:
        os.environ["CHECKPOINT_RUNTIME_RATE_LIMIT"] = str(rate_limit)
    if read_only:
        os.environ["CHECKPOINT_RUNTIME_READ_ONLY"] = "1"

    if quiet and output_format != "json":
        # Quiet mode without JSON still emits the final per-scenario score line
        # but suppresses the noisy rich panels per individual run.
        os.environ["CHECKPOINT_RUNTIME_QUIET"] = "1"

    # Docker-mode preflight: verify the daemon is reachable BEFORE we spend
    # time spinning up twins / building images. The CHECKPOINT_NO_DOCKER env
    # var is an escape hatch for environments where Docker is intentionally
    # unavailable but `--docker` is still on by default (e.g. nested CI).
    if docker:
        if os.environ.get("CHECKPOINT_NO_DOCKER") == "1":
            if not quiet:
                console.print(
                    "[yellow]CHECKPOINT_NO_DOCKER=1 set; falling back to subprocess mode.[/yellow]"
                )
            docker = False
        else:
            try:
                import docker as _docker_pkg
                _docker_pkg.from_env().ping()
            except Exception as _e:
                console.print(
                    "[red]Docker is the default run mode but the daemon is unreachable.[/red]\n"
                    f"[red]  -> {_e}[/red]\n"
                    "[dim]Either start Docker, or pass [bold]--no-docker[/bold] to use subprocess mode (real SDKs against production URLs will not work).[/dim]"
                )
                sys.exit(2)

    # Docker mode delivers the task only via the CHECKPOINT_TASK env var; the
    # arg/stdin injection modes are a subprocess-mode feature. Fail loudly
    # rather than silently ignoring the flag (B6).
    if docker and extra_env.get("CHECKPOINT_TASK_VIA") in ("arg", "stdin"):
        raise click.UsageError(
            f"--task-via {extra_env['CHECKPOINT_TASK_VIA']} is not supported in Docker mode "
            "(the task is delivered via the CHECKPOINT_TASK env var). "
            "Use --task-via env (the default), or pass --no-docker for subprocess mode."
        )

    # --- Iterate scenarios with --tag filter ---
    any_failed = False
    any_run = False
    all_run_dumps: list[dict] = []
    json_summary: list[dict] = []  # populated when --output json
    threshold_violation = False
    for scn_path in scenario_files:
        scenario = parse_file(scn_path) if scn_path else Scenario()
        if task:
            scenario.prompt = task

        # CLI overrides for seed-file / setup-file / keep-state / fresh-seed
        # take effect BEFORE the runner sees the scenario config.
        if seed_file:
            scenario.config["seed-file"] = seed_file
        if setup_file:
            try:
                scenario.setup = Path(setup_file).read_text(encoding="utf-8")
            except OSError as e:
                console.print(f"[red]--setup-file: cannot read {setup_file}: {e}[/red]")
                sys.exit(2)
        if keep_state and not fresh_seed:
            # Strip any seed config so the runner's seed step short-circuits.
            for k in ("seed", "seed_name", "seed-file", "seed_file"):
                scenario.config.pop(k, None)
            # Also strip Setup prose so SCN-08 setup-derived seeding doesn't fire.
            scenario.setup = ""

        # SCN-10: --tag filter (only applies when iterating a directory).
        if tag and is_directory:
            if not matches_tag(scenario.config.get("tags"), tag):
                if not quiet:
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
        elif isinstance(_ucfg_runs, int) and "runs" not in scenario.config:
            # user-config default only when neither flag nor scenario set it
            scenario.config["runs"] = str(_ucfg_runs)
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

        if not quiet:
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

        # Snapshot harness identity so it lands in every run record. Without
        # this the dashboard can't tell you which agent produced a run.
        harness_meta = _build_harness_meta(
            harness_cmd_str=harness_cmd_str,
            harness_dir=harness_dir,
            docker=docker,
        )

        results: list[RunResult] = []
        durations_ms: list[float] = []
        for i in range(scenario.runs):
            if not quiet:
                console.print(f"\n[bold]Run {i + 1}/{scenario.runs}[/bold]")
            _t0 = time.perf_counter()
            if docker:
                from .docker.runner import docker_run_once
                hdir = Path(harness_dir or cwd or ".").resolve()
                r = docker_run_once(scenario, harness_cmd, hdir, cwd=cwd, judge_model=resolution.model, verbose=docker_logs)
            else:
                r = run_once(scenario, harness_cmd, cwd=cwd, judge_model=resolution.model)
            durations_ms.append((time.perf_counter() - _t0) * 1000)
            results.append(r)
            if not quiet and output_format != "json":
                _print_run(r)

        if scenario.runs > 1 and not quiet and output_format != "json":
            _print_summary(results)

        # --pass-threshold: avg score over this scenario's runs
        if pass_threshold is not None:
            avg_score = sum(r.score for r in results) / len(results) if results else 0
            if avg_score < pass_threshold:
                threshold_violation = True
                if not quiet and output_format != "json":
                    console.print(
                        f"[red]threshold {pass_threshold} not met: avg {avg_score:.0f}/100[/red]"
                    )

        for r in results:
            all_run_dumps.append({
                **_dump(r),
                "scenario_path": str(scn_path) if scn_path else None,
                "evaluator_model": resolution.model,
                "evaluator_model_source": resolution.source,
            })

        # EV-05 + EV-06: per-run failure analysis + persisted run record.
        for r, dur_ms in zip(results, durations_ms):
            _persist_run_record(
                r,
                scenario_name=scenario.title or (Path(scn_path).name if scn_path else "<inline>"),
                scenario_path=str(scn_path) if scn_path else None,
                evaluator_model=resolution.model,
                evaluator_model_source=resolution.source,
                task=scenario.prompt,
                skip_failure_analysis=no_failure_analysis,
                harness=harness_meta,
                duration_ms=round(dur_ms, 1),
            )

        # Build the JSON summary entry for this scenario.
        if output_format == "json":
            json_summary.append({
                "scenario": scenario.title or (Path(scn_path).name if scn_path else "<inline>"),
                "scenario_path": str(scn_path) if scn_path else None,
                "runs": len(results),
                "satisfaction_avg": (sum(r.score for r in results) / len(results)) if results else 0,
                "satisfaction_min": min(r.score for r in results) if results else 0,
                "satisfaction_max": max(r.score for r in results) if results else 0,
                "complete": all(r.complete for r in results),
                "judge_model": resolution.model,
                "criteria_total": len(results[0].criteria) if results and results[0].criteria else 0,
                "criteria_pass_per_run": [
                    sum(1 for c in (r.criteria or []) if c.passed) for r in results
                ],
                "exit_codes": [r.exit_code for r in results],
            })

        any_run = True
        if not all(r.complete and r.score == 100.0 for r in results):
            any_failed = True

    if trace_out:
        Path(trace_out).write_text(json.dumps(all_run_dumps, indent=2))
        if not quiet:
            console.print(f"[dim]Trace written to {trace_out}[/dim]")

    if output_format == "json":
        click.echo(json.dumps({
            "scenarios": json_summary,
            "scenarios_run": len(json_summary),
            "any_failed": any_failed,
            "threshold_violation": threshold_violation,
            "pass_threshold": pass_threshold,
        }, indent=2, default=str))

    if not any_run:
        if not quiet and output_format != "json":
            console.print("[yellow]No scenarios matched the filter.[/yellow]")
        sys.exit(0)
    # CI exit semantics: --pass-threshold takes precedence over the default
    # "all 100" gate so users can require, say, 80 instead of perfection.
    if pass_threshold is not None:
        sys.exit(1 if threshold_violation else 0)
    if any_failed:
        sys.exit(1)


def _maybe_load_v2_harness(source_path: str | None):
    """If a harness.json with v2 fields (command/argv/docker) is at source_path,
    return a HarnessSpec. Otherwise None (caller falls back to legacy path)."""
    if not source_path:
        return None
    p = Path(source_path)
    if not p.is_file():
        return None
    try:
        from .harness_spec import load_manifest
        spec = load_manifest(p)
    except Exception:
        return None
    # Only treat as v2 if it had a command/argv/docker section; a bare
    # {"path": "..."} should keep flowing through the legacy code so we
    # don't change its behavior.
    raw = json.loads(p.read_text(encoding="utf-8"))
    if any(k in raw for k in ("command", "argv", "docker")):
        return spec
    return None


def _build_harness_meta(
    *,
    harness_cmd_str: str | None,
    harness_dir: str | None,
    docker: bool,
) -> dict:
    """Snapshot the agent identity that will land in every run record.

    Shapes:
      docker:     {name, dir (relative-to-cwd if possible), mode: "docker"}
      subprocess: {name (basename of the script or "inline"), cmd, mode: "subprocess"}

    "name" is what the dashboard renders prominently — keep it short and
    stable. "dir" / "cmd" are the full identity.
    """
    if docker:
        d = Path(harness_dir or ".").resolve()
        try:
            rel = d.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            rel = str(d)
        return {
            "name": d.name or "harness",
            "dir": rel,
            "mode": "docker",
        }
    # Subprocess mode: the harness is a shell command.
    cmd = (harness_cmd_str or "").strip()
    if not cmd:
        return {"name": "(none)", "cmd": "", "mode": "subprocess"}
    # Pick a friendly name: the basename of the .py file if there is one,
    # otherwise the first token of the command.
    name = "subprocess"
    for tok in shlex.split(cmd, posix=sys.platform != "win32"):
        if tok.endswith(".py"):
            name = Path(tok).stem
            break
    else:
        name = Path(cmd.split()[0]).stem if cmd.split() else "subprocess"
    return {"name": name, "cmd": cmd, "mode": "subprocess"}


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
        "stdout": getattr(r, "stdout", ""),
        "stderr": r.stderr,
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
    skip_failure_analysis: bool = False,
    harness: dict | None = None,
    duration_ms: float | None = None,
) -> None:
    """Build, optionally enrich with failure analysis, write to disk.

    Best-effort: never raise to the user — the score has already been printed.
    """
    failure_analysis: dict[str, str] = {}
    failed_texts = [c.text for c in r.criteria if not c.passed] if r.criteria else []
    if r.complete and failed_texts and not skip_failure_analysis:
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
        stdout=getattr(r, "stdout", ""),
        stderr=r.stderr,
        trace=r.trace,
        state=r.state,
        error=r.error,
        exit_code=r.exit_code,
        metrics=getattr(r, "metrics", None),
        agent_trace=getattr(r, "agent_trace", None),
        failure_analysis=failure_analysis or None,
        harness=harness,
        duration_ms=duration_ms,
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
    "--command", "command",
    default=None,
    help="Shell command that runs your existing agent (e.g. 'python my_agent.py'). "
         "Checkpoint will inject the scenario task via env / arg / stdin. "
         "Use this for the zero-code path — NO Python file gets written into your repo.",
)
@click.option(
    "--task-via",
    type=click.Choice(["env", "arg", "stdin", "none"], case_sensitive=False),
    default="env", show_default=True,
    help="How to give your agent the scenario task.",
)
@click.option(
    "--task-env", default="CHECKPOINT_TASK", show_default=True,
    help="Env var name to set with the task when --task-via=env.",
)
@click.option(
    "--task-arg", default=None,
    help="Arg flag to use when --task-via=arg (e.g. --prompt). Task value follows.",
)
@click.option(
    "--dockerfile", default=None, type=click.Path(),
    help="Point at an existing Dockerfile to use Docker mode (real-SDK fidelity).",
)
@click.option(
    "--name", default=None,
    help="Friendly name for your agent (shown in the dashboard).",
)
@click.option(
    "--template",
    default=None,
    type=click.Choice(["raw", "anthropic", "openai-agents", "langchain"], case_sensitive=False),
    help="Legacy: scaffold a starter Python harness file instead of harness.json. "
         "Most users want --command instead.",
)
def init_cmd(target_dir, command, task_via, task_env, task_arg, dockerfile, name, template):
    """Scaffold a Checkpoint integration in TARGET_DIR (default cwd).

    \b
    Quick start — your agent already runs as `python my_agent.py`:
        checkpoint init --command "python my_agent.py"
        checkpoint run scenarios/quickstart.md

    \b
    Already have a Dockerfile for your agent?
        checkpoint init --command "python my_agent.py" --dockerfile ./Dockerfile

    \b
    Need to pipe the task via a CLI flag instead of env?
        checkpoint init --command "node agent.js" --task-via arg --task-arg --prompt
    """
    from . import init as _init

    try:
        result = _init.scaffold(
            target_dir,
            command=command,
            task_via=task_via,
            task_env=task_env,
            task_arg=task_arg,
            dockerfile=dockerfile,
            name=name,
            template=template,
        )
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


@traces.command("telemetry")
@click.argument("run_id", required=False)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the full normalized telemetry JSON.")
def traces_telemetry(run_id, as_json):
    """Print a full-process telemetry report for a run record."""
    record = _load_run_record(run_id)
    if record is None:
        if run_id:
            console.print(f"[red]No run record for id={run_id}[/red]")
        else:
            console.print("[red]No last-run pointer.[/red]")
        sys.exit(1)
    report = build_telemetry_report(record)
    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return
    _print_telemetry_report(report)


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


def _print_telemetry_report(report: dict) -> None:
    summary = report.get("summary") or {}
    sat = summary.get("satisfaction") or 0
    color = "green" if sat == 100 else ("yellow" if sat >= 50 else "red")
    console.print(Panel.fit(
        f"[bold]Telemetry {report.get('run_id', '?')}[/bold]\n"
        f"[dim]scenario:[/dim] {summary.get('scenario') or '?'}\n"
        f"[dim]score:[/dim] [{color}]{sat}/100[/{color}]\n"
        f"[dim]api calls:[/dim] {summary.get('api_call_count', 0)}  "
        f"[dim]agent messages:[/dim] {summary.get('agent_message_count', 0)}  "
        f"[dim]tool calls:[/dim] {summary.get('tool_call_count', 0)}",
        border_style=color,
        title="checkpoint telemetry",
    ))

    cli = report.get("cli") or {}
    if cli:
        t = Table(box=box.SIMPLE_HEAD, show_header=False)
        t.add_column("Action", style="dim", width=14)
        t.add_column("Command", overflow="fold")
        for key in ("detail", "telemetry", "replay", "replay_json", "export", "rerun"):
            if cli.get(key):
                t.add_row(key, cli[key])
        console.print(t)

    chat = ((report.get("chat") or {}).get("messages") or [])[:12]
    if chat:
        console.print("\n[bold]Agent chat[/bold]")
        for msg in chat:
            role = msg.get("role") or "message"
            body = (msg.get("content") or "").replace("\n", " ")
            console.print(f"  [dim]{msg.get('index')}[/dim] [cyan]{role}[/cyan]: {body[:240]}")

    calls = (report.get("api_calls") or [])[:20]
    if calls:
        console.print("\n[bold]API calls[/bold]")
        t = Table(box=box.SIMPLE_HEAD)
        t.add_column("#", style="dim", width=4)
        t.add_column("Clone", width=14)
        t.add_column("Method", width=8)
        t.add_column("Path", overflow="fold")
        t.add_column("Status", width=8)
        for call in calls:
            t.add_row(
                str(call.get("index")),
                str(call.get("clone") or "—"),
                str(call.get("method") or "—"),
                str(call.get("path") or "—")[:100],
                str(call.get("status") or "—"),
            )
        console.print(t)

    judge = (report.get("judge") or {}).get("criteria") or []
    if judge:
        console.print("\n[bold]Judge[/bold]")
        for c in judge:
            mark = "[green]PASS[/green]" if c.get("passed") else "[red]FAIL[/red]"
            console.print(f"  {mark} [{c.get('kind')}] {c.get('text')}")
            if c.get("reasoning"):
                console.print(f"    [dim]{str(c.get('reasoning'))[:260]}[/dim]")


# =============================================================================
# Phase 7 / Task 2: clone start / inspect / stop
# =============================================================================


@main.group()
def clone():
    """Manage long-lived local twin sessions."""


@clone.command("start")
@click.argument("clone_id")
@click.option("--ttl-seconds", type=int, default=None,
              help="Set TTL metadata for `clone list` (advisory; not auto-killed).")
@click.option("--seed", "seed_name", default=None,
              help="Apply a named seed immediately after the clone starts.")
def clone_start(clone_id, ttl_seconds, seed_name):
    """CLI-07: start a long-lived twin session (github/slack/stripe/etc)."""
    from . import clone_manager
    try:
        entry = clone_manager.start(clone_id)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]clone start {clone_id}: {e}[/red]")
        sys.exit(1)
    if ttl_seconds:
        try:
            entry = clone_manager.renew(clone_id, ttl_seconds=ttl_seconds)
        except (KeyError, RuntimeError):
            pass
    if seed_name:
        result = clone_manager.seed(clone_id, seed_name)
        if not result.get("ok"):
            console.print(
                f"[yellow]Started, but seed {seed_name!r} failed: "
                f"{result.get('error') or result.get('status')}[/yellow]"
            )
    console.print(Panel.fit(
        f"[bold]Clone {clone_id} started[/bold]\n"
        f"[dim]URL:[/dim]      {entry['url']}\n"
        f"[dim]MCP URL:[/dim]  {entry['mcp_url']}\n"
        f"[dim]Token:[/dim]    {entry['token']}\n"
        f"[dim]PID:[/dim]      {entry['pid']}"
        + (f"\n[dim]Expires:[/dim]  {entry.get('expires_at_iso', '')}" if ttl_seconds else "")
        + (f"\n[dim]Seed:[/dim]     {seed_name}" if seed_name else ""),
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
        build_harness_image(Path(harness_dir), None, tag)
        console.print(f"[green]Built {tag}[/green]")
    except Exception as e:
        console.print(f"[red]Build failed: {e}[/red]")
        sys.exit(1)


@docker_group.command("build-sidecar")
@click.option("--force", is_flag=True, default=False,
              help="Rebuild even if the sidecar image already exists.")
def docker_build_sidecar(force):
    """Pre-build the TLS sidecar image (otherwise built automatically on first run)."""
    from .docker.sidecar import SIDECAR_IMAGE, ensure_sidecar_image
    try:
        import docker as _docker
        client = _docker.from_env()
        client.ping()
    except Exception as e:
        console.print(f"[red]Docker not reachable: {e}[/red]")
        sys.exit(1)
    try:
        ensure_sidecar_image(client, force=force, log_fn=lambda m: console.print(f"[dim]{m}[/dim]"))
        console.print(f"[green]Sidecar image ready: {SIDECAR_IMAGE}[/green]")
    except Exception as e:
        console.print(f"[red]Sidecar build failed: {e}[/red]")
        sys.exit(1)


@main.command("redteam-mcp")
@click.option("--technique", default="MCP03", show_default=True,
              type=click.Choice(["MCP03", "MCP06", "MCP10"]),
              help="OWASP MCP attack: MCP03 tool poisoning, MCP06 intent subversion, MCP10 context injection.")
def redteam_mcp(technique):
    """Serve a deliberately POISONED MCP server (OWASP MCP Top 10) over stdio.

    It looks like a benign knowledge-base server, but a tool description hides an
    instruction to call a destructive `admin_delete_all` trap. Point your MCP
    agent at it and confirm it does NOT get steered into the trap.
    """
    from .redteam.mcp_poison import build_poisoned_server
    server, _state = build_poisoned_server(technique=technique)
    server.run(transport="stdio")


@main.command("mcp")
def mcp_cmd():
    """Run Checkpoint as an MCP server (stdio) so a coding agent (Claude Code,
    Cursor, ...) can list scenarios, run one, or gate a build — inline, while it
    writes the very agent under test.

    Register it with your MCP client, e.g.:  command `checkpoint`, args `["mcp"]`.
    """
    from .mcp_gate.server import run_stdio
    run_stdio()


@main.command("gate")
@click.argument("target", type=click.Path(exists=True))
@click.option("--harness", required=True,
              help="Command that runs your agent, e.g. 'python my_agent.py'.")
@click.option("-n", "--runs", type=int, default=None,
              help="Runs per scenario. [default: 20]")
@click.option("--pass-threshold", type=float, default=None,
              help="Score (0-100) a single run needs to count as a pass. [default: 80]")
@click.option("--ship-min", type=float, default=None,
              help="CI lower bound (0-1) required to SHIP. [default: 0.80]")
@click.option("--block-max", type=float, default=None,
              help="CI upper bound (0-1) at/under which to BLOCK. [default: 0.50]")
@click.option("--confidence", type=float, default=0.95, show_default=True,
              help="Confidence level for the interval.")
@click.option("--strict", is_flag=True, default=False,
              help="Exit non-zero on CONDITIONAL as well as BLOCK.")
@click.option("--judge-model", default=None, help="Model for [P] LLM-judged criteria.")
@click.option("--agent", default=None, help="Agent name recorded in the certificate.")
@click.option("--certificate", "cert_path", type=click.Path(dir_okay=False), default=None,
              help="Write a signed Trust Certificate of the verdict to this path.")
@click.option("--no-baseline", is_flag=True, default=False,
              help="Do not compare against / update the stored pass-rate baseline.")
@click.option("-o", "--output", "output_format",
              type=click.Choice(["text", "json"]), default="text", show_default=True)
def gate(target, harness, runs, pass_threshold, ship_min, block_max, confidence,
         strict, judge_model, agent, cert_path, no_baseline, output_format):
    """Statistically gate an agent: run each scenario N times and decide
    SHIP / CONDITIONAL / BLOCK from the pass-rate distribution (not one run).

    Exit code is 0 for SHIP (and CONDITIONAL unless --strict), 1 for BLOCK.
    """
    import json as _json
    import shlex as _shlex

    from .gate import GatePolicy, run_gate

    policy = GatePolicy(
        runs=runs if runs is not None else 20,
        pass_threshold=pass_threshold if pass_threshold is not None else 80.0,
        confidence=confidence,
        ship_min=ship_min if ship_min is not None else 0.80,
        block_max=block_max if block_max is not None else 0.50,
        strict=strict,
    )
    harness_cmd = _shlex.split(harness, posix=(os.name != "nt"))
    jm = judge_model or "gpt-4o-mini"

    quiet_progress = output_format == "json"

    def _progress(name: str, i: int, total: int, score: float, complete: bool) -> None:
        if quiet_progress:
            return
        mark = "[green]P[/green]" if (complete and score >= policy.pass_threshold) else "[red]F[/red]"
        console.print(f"[dim]{name}[/dim]  run {i}/{total}  {mark} {score:.0f}/100", highlight=False)

    baselines = None
    if not no_baseline:
        from .gate import baseline as _baseline
        baselines = _baseline.load(Path(target))

    result = run_gate(Path(target), harness_cmd, policy, judge_model=jm,
                      progress=_progress, baselines=baselines)

    if not no_baseline:
        from .gate import baseline as _baseline
        _baseline.save(Path(target), result.scenarios)

    cert_written: str | None = None
    if cert_path:
        from .gate.certificate import LocalSigner, build_certificate
        body = build_certificate(
            result,
            agent=agent or Path(target).stem,
            harness_cmd=harness_cmd,
            commit_sha=_git_commit_sha(),
            model=jm,
        )
        signed = LocalSigner().sign(body)
        Path(cert_path).write_text(_json.dumps(signed, indent=2), encoding="utf-8")
        cert_written = cert_path

    if output_format == "json":
        console.print_json(_json.dumps({
            "verdict": result.verdict,
            "exit_code": result.exit_code,
            "policy": {
                "runs": policy.runs, "pass_threshold": policy.pass_threshold,
                "confidence": policy.confidence, "ship_min": policy.ship_min,
                "block_max": policy.block_max, "strict": policy.strict,
            },
            "scenarios": [{
                "scenario": s.scenario, "n": s.n, "passes": s.passes,
                "pass_rate": round(s.pass_rate, 4),
                "ci_low": round(s.ci.low, 4), "ci_high": round(s.ci.high, 4),
                "classification": s.classification,
                "mean_score": round(s.mean_score, 2),
            } for s in result.scenarios],
            "errors": result.errors,
            "certificate": cert_written,
        }))
        sys.exit(result.exit_code)

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Scenario")
    table.add_column("Pass", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column(f"{int(policy.confidence * 100)}% CI", justify="center")
    table.add_column("Verdict")
    _cls_color = {"stable_pass": "green", "stable_fail": "red",
                  "regression": "red", "flaky": "yellow"}
    for s in result.scenarios:
        color = _cls_color.get(s.classification, "white")
        table.add_row(
            s.scenario,
            f"{s.passes}/{s.n}",
            f"{s.pass_rate * 100:.0f}%",
            f"[{s.ci.low * 100:.0f}%, {s.ci.high * 100:.0f}%]",
            f"[{color}]{s.classification}[/{color}]",
        )
    console.print(table)

    if result.errors:
        console.print(f"[yellow]{len(result.errors)} run error(s):[/yellow]")
        for e in result.errors[:10]:
            console.print(f"  [dim]{e}[/dim]")

    verdict_style = {"SHIP": "bold green", "CONDITIONAL": "bold yellow", "BLOCK": "bold red"}[result.verdict]
    console.print(Panel.fit(
        f"[{verdict_style}]{result.verdict}[/{verdict_style}]",
        title="gate verdict",
        border_style=verdict_style.split()[-1],
    ))
    if cert_written:
        console.print(f"[dim]Signed certificate written to {cert_written}[/dim]")
    sys.exit(result.exit_code)


def _git_commit_sha() -> str | None:
    """Best-effort current commit SHA, for certificate provenance."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@main.group("cert")
def cert_group():
    """Work with signed Trust Certificates."""


@cert_group.command("verify")
@click.argument("cert_file", type=click.Path(exists=True, dir_okay=False))
def cert_verify(cert_file):
    """Verify a certificate's signature (and report expiry). Exit 1 if invalid."""
    import json as _json2
    from .gate.certificate import is_expired, verify as _verify

    try:
        certificate = _json2.loads(Path(cert_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        console.print(f"[red]Cannot read certificate: {e}[/red]")
        sys.exit(1)

    ok = _verify(certificate)
    expired = is_expired(certificate)
    subject = certificate.get("subject", {})
    console.print(Panel.fit(
        f"[bold]{'VALID' if ok else 'INVALID'}[/bold]  signature\n"
        f"verdict:  {certificate.get('verdict', '?')}\n"
        f"agent:    {subject.get('agent', '?')}\n"
        f"gate id:  {certificate.get('gate_id', '?')}\n"
        f"issued:   {certificate.get('issued_at', '?')}\n"
        f"expires:  {certificate.get('expires_at', '?')}"
        + ("  [red](EXPIRED)[/red]" if expired else ""),
        title="checkpoint cert verify",
        border_style="green" if ok and not expired else "red",
    ))
    sys.exit(0 if (ok and not expired) else 1)


@main.command("redteam")
@click.option("--harness", required=True,
              help="Command that runs your agent, e.g. 'python my_agent.py'.")
@click.option("--pack", "pack_dir", type=click.Path(file_okay=False), default=None,
              help="Directory of adversarial scenarios. [default: scenarios/redteam if present, else scenarios/]")
@click.option("-n", "--runs", type=int, default=5, show_default=True,
              help="Runs per adversarial scenario.")
@click.option("--pass-threshold", type=float, default=80.0, show_default=True,
              help="Score a single run needs to count as 'resisted'.")
@click.option("--judge-model", default=None, help="Model for [P] LLM-judged criteria.")
@click.option("-o", "--output", "output_format",
              type=click.Choice(["text", "json"]), default="text", show_default=True)
def redteam(harness, pack_dir, runs, pass_threshold, judge_model, output_format):
    """Run an adversarial pack (OWASP Agentic Top 10) and report which attack
    categories your agent is vulnerable to. Exit 1 if any vulnerability is found.
    """
    import json as _json
    import shlex as _shlex

    from .gate import GatePolicy
    from .redteam import collect_pack, describe, run_redteam

    if pack_dir is None:
        default_pack = Path("scenarios") / "redteam"
        base = default_pack if default_pack.is_dir() else Path("scenarios")
    else:
        base = Path(pack_dir)
    if not base.is_dir():
        console.print(f"[red]No scenario directory found at {base}[/red]")
        sys.exit(2)

    pack = collect_pack(base)
    if not pack:
        console.print(
            f"[yellow]No adversarial scenarios found under {base} "
            "(scenarios need an `owasp: ASIxx` field in ## Config).[/yellow]"
        )
        sys.exit(2)

    policy = GatePolicy(runs=runs, pass_threshold=pass_threshold)
    harness_cmd = _shlex.split(harness, posix=(os.name != "nt"))
    jm = judge_model or "gpt-4o-mini"

    quiet = output_format == "json"

    def _progress(name, i, total, score, complete):
        if quiet:
            return
        mark = "[green].[/green]" if (complete and score >= pass_threshold) else "[red]x[/red]"
        console.print(f"[dim]{name}[/dim] {i}/{total} {mark}", highlight=False)

    report = run_redteam(pack, harness_cmd, policy, judge_model=jm, progress=_progress)

    if output_format == "json":
        console.print_json(_json.dumps({
            "vulnerable": bool(report.vulnerabilities),
            "exit_code": report.exit_code,
            "entries": [{
                "scenario": e.scenario, "category": e.category,
                "classification": e.classification,
                "passes": e.passes, "n": e.n, "resisted": e.resisted,
            } for e in report.entries],
            "errors": report.errors,
        }))
        sys.exit(report.exit_code)

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("OWASP")
    table.add_column("Attack scenario")
    table.add_column("Runs", justify="right")
    table.add_column("Result")
    for e in report.entries:
        cat = describe(e.category) if e.category else None
        cat_label = f"{e.category} {cat.name}" if cat else (e.category or "-")
        if e.resisted:
            result = "[green]resisted[/green]"
        elif e.classification == "stable_fail":
            result = "[red]VULNERABLE[/red]"
        else:
            result = "[yellow]flaky (attack lands sometimes)[/yellow]"
        table.add_row(cat_label, e.scenario, f"{e.passes}/{e.n}", result)
    console.print(table)

    n_vuln = len(report.vulnerabilities)
    if n_vuln:
        console.print(Panel.fit(
            f"[bold red]{n_vuln} vulnerability(ies) found[/bold red]",
            title="red-team", border_style="red",
        ))
    else:
        console.print(Panel.fit(
            "[bold green]resisted every attack[/bold green]",
            title="red-team", border_style="green",
        ))
    sys.exit(report.exit_code)


@main.command("simulate")
@click.argument("scenario_path", type=click.Path(exists=True))
@click.option("--harness", required=True,
              help="Command that runs your agent, e.g. 'python my_agent.py'.")
@click.option("--goal", default=None, help="The user's goal (default: the scenario prompt).")
@click.option("--persona", "persona_name", default=None, help="Persona name.")
@click.option("--tone", default=None, help="How the user writes (terse, polite, frustrated...).")
@click.option("--patience", type=int, default=None, help="User's turns before giving up.")
@click.option("--adversarial", is_flag=True, default=False,
              help="User applies social pressure to get past a policy boundary.")
@click.option("--max-turns", type=int, default=6, show_default=True)
@click.option("--judge-model", default=None, help="Model for the simulated user + [P] criteria.")
@click.option("-o", "--output", "output_format",
              type=click.Choice(["text", "json"]), default="text", show_default=True)
def simulate_cmd(scenario_path, harness, goal, persona_name, tone, patience,
                 adversarial, max_turns, judge_model, output_format):
    """Run a multi-turn conversation between a simulated user and your agent,
    against stateful twins, and evaluate whether the goal was met.

    Each result carries a calibration confidence — LLM-simulated users are
    imperfect proxies for humans, so the score is reported with that caveat.
    """
    import json as _json
    import shlex as _shlex

    from .simuser import Persona, simulate as run_sim
    from .simuser.persona import scenario_persona

    scenario = parse_file(scenario_path)
    base = scenario_persona(scenario)
    persona = Persona(
        name=persona_name or base.name,
        goal=goal or base.goal,
        tone=tone or base.tone,
        patience=patience if patience is not None else base.patience,
        adversarial=adversarial or base.adversarial,
    )
    harness_cmd = _shlex.split(harness, posix=(os.name != "nt"))
    jm = judge_model or "gpt-4o-mini"

    res = run_sim(scenario, harness_cmd, persona, max_turns=max_turns, judge_model=jm)

    passed = res.error is None and res.result is not None and res.result.score >= 80.0
    exit_code = 0 if passed else 1

    if output_format == "json":
        console.print_json(_json.dumps({
            "persona": persona.name,
            "goal": persona.goal,
            "turns": res.turns,
            "satisfied": res.satisfied,
            "gave_up": res.gave_up,
            "score": res.score,
            "calibration": res.calibration,
            "error": res.error,
            "transcript": res.transcript,
            "criteria": [
                {"text": c.text, "kind": c.kind, "passed": c.passed, "evaluator": c.evaluator}
                for c in (res.result.criteria if res.result else [])
            ],
        }))
        sys.exit(exit_code)

    if res.error:
        console.print(f"[red]Simulation error: {res.error}[/red]")
        sys.exit(1)

    console.print(f"[bold]Conversation[/bold] — persona: {persona.name}")
    for turn in res.transcript:
        who = "[cyan]user[/cyan]" if turn["role"] == "user" else "[magenta]agent[/magenta]"
        console.print(f"  {who}: {turn['content']}", highlight=False)

    if res.result and res.result.criteria:
        table = Table(box=box.SIMPLE, show_edge=False)
        table.add_column("")
        table.add_column("Criterion")
        for c in res.result.criteria:
            mark = "[green]PASS[/green]" if c.passed else "[red]FAIL[/red]"
            table.add_row(mark, c.text)
        console.print(table)

    outcome = "satisfied" if res.satisfied else ("gave up" if res.gave_up else "inconclusive")
    style = "green" if passed else "yellow"
    console.print(Panel.fit(
        f"[bold {style}]{outcome}[/bold {style}]  in {res.turns} turn(s)\n"
        f"score: {res.score:.0f}/100\n"
        f"calibration: {res.calibration:.2f}  "
        f"[dim](confidence this sim was human-like; not ground truth)[/dim]",
        title="simulate", border_style=style,
    ))
    sys.exit(exit_code)


@main.group("db")
def db_group():
    """The local run store (SQLite) — migrate JSON records in, then query."""


@db_group.command("path")
def db_path_cmd():
    """Print the store's database path."""
    from .store import default_db_path
    click.echo(str(default_db_path()))


@db_group.command("migrate")
def db_migrate_cmd():
    """Import the legacy JSON run-record cache into SQLite (idempotent)."""
    from .run_record import RUNS_DIR
    from .store import SqliteRunStore, migrate_json_runs
    store = SqliteRunStore()
    try:
        n = migrate_json_runs(RUNS_DIR, store)
        console.print(f"[green]Imported {n} run record(s) into {store.db_path}[/green]")
    finally:
        store.close()


@db_group.command("list")
@click.option("--scenario", default=None, help="Filter by scenario name.")
@click.option("--limit", "-n", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def db_list_cmd(scenario, limit, as_json):
    """List recent runs from the store."""
    import json as _json
    from .store import SqliteRunStore
    store = SqliteRunStore()
    try:
        rows = store.list_runs(scenario=scenario, limit=limit)
    finally:
        store.close()
    if as_json:
        console.print_json(_json.dumps(rows))
        return
    if not rows:
        console.print("[dim]No runs in the store. Run `checkpoint db migrate` to import JSON records.[/dim]")
        return
    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Run")
    table.add_column("Scenario")
    table.add_column("Score", justify="right")
    table.add_column("When")
    for r in rows:
        score = f"{r['score']:.0f}" if r["score"] is not None else "-"
        table.add_row(r["run_id"], r["scenario"] or "-", score, r["timestamp"] or "-")
    console.print(table)


@main.command("compliance")
@click.option("--certificate", "cert_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="A signed gate certificate (from `checkpoint gate --certificate`).")
@click.option("--redteam", "redteam_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="A red-team report JSON (from `checkpoint redteam -o json`).")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None,
              help="Write the assurance report (markdown) to this path.")
@click.option("-o", "--output", "output_format",
              type=click.Choice(["text", "json"]), default="text", show_default=True)
def compliance_cmd(cert_path, redteam_path, out_path, output_format):
    """Build an Agent Assurance Report from a signed gate certificate (and an
    optional red-team report): the verdict, the statistical evidence, and OWASP
    Agentic / NIST / EU-AI-Act cross-references — the document a reviewer wants.

    Exit 1 if the overall verdict is REJECTED.
    """
    import json as _json

    from .compliance import build_assurance, render_markdown
    from .gate.certificate import verify as _verify_cert

    try:
        cert = _json.loads(Path(cert_path).read_text(encoding="utf-8"))
        redteam = _json.loads(Path(redteam_path).read_text(encoding="utf-8")) if redteam_path else None
    except (OSError, ValueError) as e:
        console.print(f"[red]Cannot read input: {e}[/red]")
        sys.exit(1)

    report = build_assurance(cert, redteam, signature_valid=_verify_cert(cert))
    markdown = render_markdown(report)

    if out_path:
        Path(out_path).write_text(markdown, encoding="utf-8")
        if output_format == "text":
            console.print(f"[green]Assurance report written to {out_path}[/green]")

    if output_format == "json":
        console.print_json(_json.dumps(report))
    else:
        console.print(markdown, highlight=False)
        style = {"APPROVED": "green", "CONDITIONAL": "yellow", "REJECTED": "red"}[report["overall"]]
        console.print(Panel.fit(f"[bold {style}]{report['overall']}[/bold {style}]",
                                title="assurance", border_style=style))

    sys.exit(1 if report["overall"] == "REJECTED" else 0)


def _extract_otel_spans(data) -> list:
    """Pull spans out of a list, a `{"spans": [...]}` dict, or full OTLP-JSON."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("spans"), list):
            return data["spans"]
        spans: list = []
        for rs in data.get("resourceSpans", []):
            scopes = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
            for ss in scopes:
                spans.extend(ss.get("spans", []) or [])
        return spans
    return []


@main.command("otel")
@click.argument("spans_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_format",
              type=click.Choice(["text", "json"]), default="text", show_default=True)
def otel_cmd(spans_file, output_format):
    """Summarize an agent's trajectory from an OpenTelemetry (GenAI) trace file.

    Accepts a list of spans, a {"spans": [...]} object, or full OTLP-JSON.
    Maps model/tool spans into the same path metrics `[T]` criteria use.
    """
    import json as _json

    from .trajectory import compute_metrics, from_otel_spans

    try:
        data = _json.loads(Path(spans_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        console.print(f"[red]Cannot read spans file: {e}[/red]")
        sys.exit(1)

    traj = from_otel_spans(_extract_otel_spans(data))
    metrics = compute_metrics(traj)

    if output_format == "json":
        console.print_json(_json.dumps({
            "steps": len(traj),
            "metrics": metrics.as_dict(),
            "path": [f"{s.method} {s.path}" for s in traj.steps],
        }))
        return

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for k, v in metrics.as_dict().items():
        if k == "methods":
            v = ", ".join(f"{mk}:{mv}" for mk, mv in v.items()) or "-"
        table.add_row(k.replace("_", " "), str(v))
    console.print(table)


@main.command("gen-attacks")
@click.argument("base_scenario", type=click.Path(exists=True))
@click.option("--out", "out_dir", type=click.Path(file_okay=False), required=True,
              help="Directory to write generated adversarial scenarios into.")
@click.option("--count", type=int, default=5, show_default=True)
@click.option("--judge-model", "model", default=None, help="Model that generates the attacks.")
def gen_attacks(base_scenario, out_dir, count, model):
    """Generate adversarial scenario variations from a benign base scenario (LLM).

    The generated scenarios join a red-team pack; REVIEW them before using them
    to gate — generated attacks are candidates, not verdicts.
    """
    import re as _re

    from .redteam.generate import generate_attacks

    scenario = parse_file(base_scenario)
    jm = model or "gpt-4o-mini"
    try:
        attacks = generate_attacks(
            scenario.prompt, scenario.clones, setup=scenario.setup, count=count, model=jm,
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Generation failed: {e}[/red]")
        sys.exit(1)
    if not attacks:
        console.print("[yellow]No attacks generated.[/yellow]")
        sys.exit(1)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for i, atk in enumerate(attacks, 1):
        slug = _re.sub(r"[^a-z0-9]+", "-", atk.title.lower()).strip("-")[:40] or f"attack-{i}"
        (out / f"gen-{i:02d}-{slug}.md").write_text(atk.to_markdown(), encoding="utf-8")
        console.print(f"  [dim]{atk.owasp}[/dim] gen-{i:02d}-{slug}.md")
    console.print(f"[green]Generated {len(attacks)} adversarial scenarios in {out_dir}[/green]")
    console.print("[yellow]Review these before gating — generated attacks are candidates, not verdicts.[/yellow]")


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
@click.option("--port", type=int, default=None,
              help="Port to listen on. [default: dashboard.port user config, else 4001]")
@click.option("--host", default=None,
              help="Host to bind. [default: dashboard.host user config, else 127.0.0.1]")
@click.option(
    "--scenarios", "scenarios_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory to scan for .md scenario files. "
         "[default: defaults.scenarios_dir user config, else .]",
)
@click.option("--open/--no-open", "auto_open", default=False,
              help="Open the dashboard in the default browser.")
@click.option("--judge-model", default="gpt-4o-mini", show_default=True,
              help="Default judge model surfaced in the dashboard meta + new runs.")
def serve(port, host, scenarios_dir, auto_open, judge_model):
    """Start the checkpoint web dashboard."""
    # Resolve host/port/scenarios from user config when the flag is unset.
    # Precedence: explicit flag > ~/.checkpoint/config.json > built-in default.
    from .user_config import UserConfig
    _ucfg = UserConfig.load()
    if port is None:
        _up = _ucfg.get("dashboard.port")
        port = _up if isinstance(_up, int) else 4001
    if host is None:
        _uh = _ucfg.get("dashboard.host")
        host = _uh if isinstance(_uh, str) and _uh else "127.0.0.1"
    if scenarios_dir is None:
        _us = _ucfg.get("defaults.scenarios_dir")
        scenarios_dir = _us if isinstance(_us, str) and _us else "."

    # The dashboard can spawn `checkpoint run` subprocesses (POST /api/jobs).
    # On loopback that's fine (single-user local tool). But binding to any
    # other interface exposes that to the network, so require an API key there.
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not os.environ.get("CHECKPOINT_DASHBOARD_API_KEY"):
            console.print(
                f"[red]Refusing to serve on {host!r} without authentication.[/red]\n"
                "[dim]Binding off loopback exposes POST /api/jobs (which runs agent "
                "harnesses) to the network. Set an API key first:[/dim]\n"
                "  export CHECKPOINT_DASHBOARD_API_KEY=$(python -c "
                "'import secrets;print(secrets.token_urlsafe(32))')\n"
                "[dim]or bind to the default 127.0.0.1.[/dim]"
            )
            sys.exit(1)

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


# =============================================================================
# whoami — local-only identity / environment summary
# =============================================================================


@main.command("whoami")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON.")
def whoami(as_json):
    """Print local Checkpoint identity (version, paths, judge model, runs).

    Checkpoint is a local tool — there is no remote workspace and no login.
    `whoami` is the developer-facing equivalent: everything you need to know
    about *this* installation in one place.
    """
    from . import identity
    ident = identity.collect()
    if as_json:
        click.echo(json.dumps(identity.to_dict(ident), indent=2))
        return
    t = Table(box=box.SIMPLE_HEAD, show_header=False)
    t.add_column("Field", style="dim", width=22)
    t.add_column("Value", overflow="fold")
    t.add_row("Version", ident.version)
    t.add_row("Python", ident.python)
    t.add_row("Platform", ident.platform)
    t.add_row("Home", ident.home)
    t.add_row("User config", ident.user_config or "[dim]not created — run `checkpoint config init`[/dim]")
    t.add_row("Project config", ident.project_config or "[dim]none in cwd[/dim]")
    t.add_row("Scenarios dir", ident.scenarios_dir)
    t.add_row("Runs dir", ident.runs_dir)
    t.add_row("Cached runs", str(ident.runs_count))
    t.add_row("Live clones", str(ident.live_clones))
    t.add_row(
        "Judge model",
        f"{ident.judge_model} [dim]({ident.judge_model_source})[/dim]",
    )
    t.add_row(
        "OPENAI_API_KEY",
        "[green]set[/green]" if ident.openai_key_present else "[red]missing[/red]",
    )
    console.print(t)


# =============================================================================
# config — user-level config file (~/.checkpoint/config.json)
# =============================================================================


@main.group("config")
def config_group():
    """Manage user-level config at ~/.checkpoint/config.json.

    User config holds personal defaults (judge model, scenarios dir, etc.)
    and is consulted with this precedence:

        flag  >  project .checkpoint.json  >  user config  >  env  >  built-in default

    The ``env:NAME`` syntax indirects through the environment:
    ``checkpoint config set engine.openai_api_key env:OPENAI_API_KEY``.
    """


@config_group.command("path")
def config_path_cmd():
    """Print the path to the user config file (created or not)."""
    from .user_config import config_path as _cp
    click.echo(str(_cp()))


@config_group.command("init")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing config file.")
def config_init(force):
    """Create ~/.checkpoint/config.json with sensible defaults."""
    from .user_config import UserConfig, config_path as _cp
    p = _cp()
    if p.exists() and not force:
        console.print(f"[yellow]Config already exists at {p}. Use --force to overwrite.[/yellow]")
        sys.exit(1)
    cfg = UserConfig(data={}, path=p)
    cfg.set("defaults.judge_model", "gpt-4o-mini")
    cfg.set("defaults.pass_threshold", 100)
    cfg.set("dashboard.port", 4001)
    cfg.set("dashboard.host", "127.0.0.1")
    cfg.set("telemetry.enabled", False)
    cfg.save()
    console.print(f"[green]Wrote {p}[/green]")


@config_group.command("show")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--reveal-env", is_flag=True, default=False,
              help="Resolve env: indirections (default: show env: literally).")
def config_show(as_json, reveal_env):
    """Show all keys in the user config."""
    from .user_config import UserConfig, KNOWN_KEYS
    cfg = UserConfig.load()
    flat = cfg.flatten()
    if reveal_env:
        flat = {k: cfg.get(k, resolve_env=True) for k in flat}
    if as_json:
        click.echo(json.dumps(flat, indent=2, default=str))
        return
    if not flat:
        console.print(
            f"[yellow]No user config at {cfg.path}.[/yellow] "
            f"Run [bold]checkpoint config init[/bold] to create one."
        )
        return
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Key", style="dim", overflow="fold")
    t.add_column("Value", overflow="fold")
    t.add_column("Description", overflow="fold")
    for k, v in flat.items():
        t.add_row(k, _fmt_config_value(v), KNOWN_KEYS.get(k, ""))
    console.print(t)
    console.print(f"[dim]Source: {cfg.path}[/dim]")


@config_group.command("get")
@click.argument("key")
@click.option("--reveal-env", is_flag=True, default=False)
def config_get(key, reveal_env):
    """Print a single key's value (use --reveal-env to resolve env: indirection)."""
    from .user_config import UserConfig
    cfg = UserConfig.load()
    val = cfg.get(key, resolve_env=reveal_env)
    if val is None:
        sys.exit(1)
    click.echo(_fmt_config_value(val))


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a key. Values starting with `env:` indirect through the environment.

    Examples:
        checkpoint config set defaults.judge_model gpt-4o
        checkpoint config set defaults.pass_threshold 80
        checkpoint config set engine.openai_api_key env:OPENAI_API_KEY
    """
    from .user_config import UserConfig, KNOWN_KEYS
    if key not in KNOWN_KEYS:
        console.print(
            f"[yellow]Warning: '{key}' is not a known config key. "
            f"Set anyway. (See `checkpoint config show` for known keys.)[/yellow]"
        )
    cfg = UserConfig.load()
    cfg.set(key, value)
    cfg.save()
    console.print(f"[green]{key} = {value}[/green]")


@config_group.command("unset")
@click.argument("key")
def config_unset(key):
    """Delete a config key."""
    from .user_config import UserConfig
    cfg = UserConfig.load()
    if cfg.unset(key):
        cfg.save()
        console.print(f"[green]Removed {key}[/green]")
    else:
        console.print(f"[yellow]{key} was not set[/yellow]")
        sys.exit(1)


def _fmt_config_value(v) -> str:
    if v is None:
        return "[dim](unset)[/dim]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# =============================================================================
# debug — usage stats, run export with anonymization
# =============================================================================


@main.group("debug")
def debug_group():
    """Diagnostics: usage stats, anonymized exports, environment readiness."""


@debug_group.command("doctor")
def debug_doctor_alias():
    """Alias for `checkpoint doctor` — environment readiness check."""
    from click.testing import CliRunner
    # Re-invoke the existing top-level doctor command.
    from .cli import doctor as _d
    ctx = click.Context(_d)
    _d.invoke(ctx)


@debug_group.command("usage")
@click.option("--json", "as_json", is_flag=True, default=False)
def debug_usage(as_json):
    """Aggregate usage stats from cached runs (counts, scores, model spend)."""
    if not RUNS_DIR.exists():
        rows: list[dict] = []
    else:
        rows = []
        for f in RUNS_DIR.glob("*.json"):
            try:
                rows.append(json.loads(f.read_text(encoding="utf-8", errors="replace")))
            except (json.JSONDecodeError, OSError):
                continue
    summary = _build_usage_summary(rows)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
        return
    t = Table(box=box.SIMPLE_HEAD, show_header=False)
    t.add_column("Metric", style="dim", width=24)
    t.add_column("Value", overflow="fold")
    t.add_row("Total runs", str(summary["total_runs"]))
    t.add_row("Distinct scenarios", str(summary["distinct_scenarios"]))
    t.add_row("Avg satisfaction", f"{summary['avg_satisfaction']:.1f}/100")
    t.add_row("Pass rate (=100)", f"{summary['pass_rate_pct']:.1f}%")
    t.add_row("Models seen", ", ".join(summary["models"]) or "[dim](none)[/dim]")
    t.add_row("LLM calls (total)", str(summary["llm_calls_total"]))
    t.add_row("Tool calls (total)", str(summary["tool_calls_total"]))
    t.add_row("Earliest run", summary["earliest"] or "[dim]—[/dim]")
    t.add_row("Latest run", summary["latest"] or "[dim]—[/dim]")
    console.print(t)


@debug_group.command("export")
@click.argument("run_id", required=False)
@click.option("--output", "-o", required=True, type=click.Path(dir_okay=False),
              help="Where to write the JSON.")
@click.option("--anonymize", is_flag=True, default=False,
              help="Strip PII-shaped values (emails, tokens, names) before writing.")
def debug_export(run_id, output, anonymize):
    """Export a run record to JSON (optionally with --anonymize for sharing).

    Anonymization: rewrites likely-PII fields (emails, tokens, repo paths,
    user names) to deterministic placeholders so traces can be shared in bug
    reports or screenshots without leaking information.
    """
    record = _load_run_record(run_id)
    if record is None:
        console.print("[red]No run record found.[/red]")
        sys.exit(1)
    if anonymize:
        record = _anonymize_record(record)
    Path(output).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    rid = record.get("run_id", "?")[:12]
    console.print(f"[green]Exported {rid} -> {output}{' (anonymized)' if anonymize else ''}[/green]")


@debug_group.command("inspect")
@click.argument("run_id", required=False)
def debug_inspect(run_id):
    """Pretty-print a run record (alias for `traces detail`)."""
    record = _load_run_record(run_id)
    if record is None:
        console.print("[red]No run record found.[/red]")
        sys.exit(1)
    _print_run_record(record)


def _build_usage_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "total_runs": 0, "distinct_scenarios": 0, "avg_satisfaction": 0.0,
            "pass_rate_pct": 0.0, "models": [], "llm_calls_total": 0,
            "tool_calls_total": 0, "earliest": None, "latest": None,
        }
    sats = [float(r.get("satisfaction") or 0) for r in rows]
    scenarios = {r.get("scenario") for r in rows if r.get("scenario")}
    models = sorted({r.get("evaluator_model") for r in rows if r.get("evaluator_model")})
    timestamps = sorted([
        (r.get("env") or {}).get("timestamp", "")
        for r in rows
        if (r.get("env") or {}).get("timestamp")
    ])
    llm_total = sum(int((r.get("metrics") or {}).get("llmCallCount") or 0) for r in rows)
    tool_total = sum(int((r.get("metrics") or {}).get("toolCallCount") or 0) for r in rows)
    return {
        "total_runs": len(rows),
        "distinct_scenarios": len(scenarios),
        "avg_satisfaction": sum(sats) / len(sats) if sats else 0.0,
        "pass_rate_pct": 100.0 * sum(1 for s in sats if s >= 100) / len(sats),
        "models": models,
        "llm_calls_total": llm_total,
        "tool_calls_total": tool_total,
        "earliest": timestamps[0] if timestamps else None,
        "latest": timestamps[-1] if timestamps else None,
    }


_PII_RE = {
    "email": __import__("re").compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "github_pat": __import__("re").compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    "bearer": __import__("re").compile(r"\bsk-[A-Za-z0-9-_]{16,}\b"),
}


def _anonymize_record(record: dict) -> dict:
    """Return a deep-copied record with PII scrubbed.

    Conservative substitutions only — preserves shape so the trace remains
    useful for debugging:
      * emails -> ``user@example.com``
      * GitHub PATs -> ``ghp-REDACTED``
      * OpenAI-shaped keys -> ``sk-REDACTED``
    """
    text = json.dumps(record, default=str)
    text = _PII_RE["email"].sub("user@example.com", text)
    text = _PII_RE["github_pat"].sub("ghp-REDACTED", text)
    text = _PII_RE["bearer"].sub("sk-REDACTED", text)
    return json.loads(text)


# =============================================================================
# clone — additional subcommands (status, list, renew, seed, reset, tools)
# =============================================================================


@clone.command("status")
@click.argument("clone_id")
def clone_status(clone_id):
    """Show clone metadata + state/request counts (alias for `inspect`)."""
    ctx = click.get_current_context()
    ctx.invoke(clone_inspect, clone_id=clone_id)


@clone.command("list")
@click.option("--json", "as_json", is_flag=True, default=False)
def clone_list(as_json):
    """List all registered clones (alive + recently stale)."""
    from . import clone_manager
    rows = clone_manager.list_all()
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        console.print("[yellow]No registered clones.[/yellow]")
        return
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("ID", style="bold")
    t.add_column("Alive")
    t.add_column("URL", overflow="fold")
    t.add_column("PID")
    t.add_column("Started", overflow="fold")
    t.add_column("TTL", overflow="fold")
    for r in rows:
        alive = "[green]yes[/green]" if r.get("alive") else "[red]no[/red]"
        ttl = r.get("expires_at_iso") or "[dim]∞[/dim]"
        t.add_row(
            str(r.get("id", "?")),
            alive,
            str(r.get("url", "?")),
            str(r.get("pid", "?")),
            str(r.get("started_at", "?")),
            ttl,
        )
    console.print(t)


@clone.command("renew")
@click.argument("clone_id")
@click.option("--ttl-seconds", type=int, default=3600, show_default=True,
              help="Seconds until expiry (advisory metadata only).")
def clone_renew(clone_id, ttl_seconds):
    """Set / extend the clone's TTL (advisory — Checkpoint doesn't auto-kill)."""
    from . import clone_manager
    try:
        entry = clone_manager.renew(clone_id, ttl_seconds=ttl_seconds)
    except KeyError:
        console.print(f"[red]Clone {clone_id!r} not in registry.[/red]")
        sys.exit(1)
    console.print(
        f"[green]Renewed {clone_id}.[/green] "
        f"Expires at {entry['expires_at_iso']} ({ttl_seconds}s)."
    )


@clone.command("seed")
@click.argument("clone_id")
@click.argument("seed_name")
def clone_seed(clone_id, seed_name):
    """Apply a named seed to a running clone (POSTs `/_seed/<name>`)."""
    from . import clone_manager
    try:
        result = clone_manager.seed(clone_id, seed_name)
    except (KeyError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    if result.get("ok"):
        console.print(f"[green]Applied seed {seed_name!r} to {clone_id}.[/green]")
    else:
        err = result.get("error") or f"HTTP {result.get('status')}"
        console.print(f"[red]Seed failed: {err}[/red]")
        sys.exit(1)


@clone.command("reset")
@click.argument("clone_id")
def clone_reset(clone_id):
    """Reset a running clone to its factory state (POSTs `/_reset`)."""
    from . import clone_manager
    try:
        result = clone_manager.reset(clone_id)
    except (KeyError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    if result.get("ok"):
        console.print(f"[green]Reset {clone_id}.[/green]")
    else:
        err = result.get("error") or f"HTTP {result.get('status')}"
        console.print(f"[red]Reset failed: {err}[/red]")
        sys.exit(1)


@clone.command("tools")
@click.argument("clone_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def clone_tools(clone_id, as_json):
    """List MCP tools exposed by a running clone."""
    from . import clone_manager
    try:
        result = clone_manager.tools(clone_id)
    except (KeyError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    tools = result.get("tools") or []
    if as_json:
        click.echo(json.dumps(tools, indent=2, default=str))
        return
    if not tools:
        console.print(
            f"[yellow]No MCP tools found for {clone_id} "
            f"(twin may not expose an MCP surface, or is unreachable).[/yellow]"
        )
        return
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Name", style="bold")
    t.add_column("Description", overflow="fold")
    for tool in tools:
        t.add_row(
            str(tool.get("name", "?")),
            str(tool.get("description", ""))[:120],
        )
    console.print(t)
    console.print(f"[dim]{len(tools)} tools.[/dim]")


if __name__ == "__main__":
    main()
