"""``checkpoint run`` — the loop you stay in while building an agent.

One scenario, one verdict, in seconds: what the agent did to the twins, which
criteria held, and why the ones that failed failed. ``checkpoint gate`` is the
same machinery run enough times to mean something; this is the fast read.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from checkpoint.runner import RunResult
from checkpoint.scenario import Scenario, parse_file

from ._shared import (
    agent_options,
    console,
    criterion_mark,
    fail,
    plain,
    project,
    resolve_agent,
    resolve_options,
    resolve_targets,
    sandbox_options,
    score_color,
)


@click.command("run")
@click.argument("targets", nargs=-1, type=click.Path())
@agent_options
@sandbox_options
@click.option("-n", "--runs", type=int, default=None,
              help="Times to run each scenario. [default: the scenario's `runs`, else 1]")
@click.option("--task", default=None, metavar="TEXT",
              help="Run this task instead of a scenario file. Nothing is scored — "
                   "use it to watch what your agent does.")
@click.option("--twins", default=None, metavar="NAMES",
              help="Twins to start for --task, comma-separated. [default: github]")
@click.option("--model", default=None, metavar="MODEL",
              help="Judge model for [P] criteria. [default: [judge] in checkpoint.toml]")
@click.option("--timeout", type=float, default=None, metavar="SECONDS",
              help="Kill the agent after this long. [default: the scenario's `timeout`]")
@click.option("--tag", default=None, metavar="TAG",
              help="Only run scenarios whose `tags:` include this one.")
@click.option("--seed", default=None, metavar="NAME",
              help="Seed every twin with this named dataset, overriding the scenario.")
@click.option("--keep-state", is_flag=True, default=False,
              help="Do not reseed the twins — continue from the last run's state.")
@click.option("--explain", is_flag=True, default=False,
              help="Ask the judge model why each failed criterion failed "
                   "(one extra call per failed run).")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Stream the agent's own output while it runs.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Only the score lines.")
@click.option("--trace-out", type=click.Path(dir_okay=False), default=None, metavar="PATH",
              help="Write every run's trace, state and criteria to this JSON file.")
def run(targets, command, url, task_via, task_env, task_arg, cwd, intercept, egress,
        allow_hosts, rate_limit, read_only, runs, task, twins, model, timeout, tag,
        seed, keep_state, explain, verbose, as_json, quiet, trace_out):
    """Run scenarios against your agent and score what it did.

    TARGETS are scenario files or directories. With none, Checkpoint runs the
    scenarios your checkpoint.toml points at (``scenarios/`` by default).

    \b
        checkpoint run                        # every scenario in the project
        checkpoint run scenarios/refund.md    # just this one
        checkpoint run -n 5 --tag payments    # five times, payments only

    Exits 1 if any run failed a criterion, 2 if something could not be set up.
    """
    proj = project()
    agent = resolve_agent(proj, command, url=url, task_via=task_via, task_env=task_env,
                          task_arg=task_arg, cwd=cwd)
    options = resolve_options(
        proj, judge_model=model, timeout=timeout, intercept=intercept, egress=egress,
        allow_hosts=allow_hosts, rate_limit=rate_limit, read_only=read_only,
        on_line=_streamer() if verbose else None,
    )

    if task:
        scenarios = [_inline_scenario(task, twins)]
    else:
        scenarios = [_load(path) for path in resolve_targets(proj, targets)]
        scenarios = [s for s in scenarios if _wanted(s, tag, quiet or as_json)]
        if not scenarios:
            fail(f"no scenario is tagged {tag!r}" if tag else "no runnable scenarios found")

    quiet = quiet or as_json
    all_results: list[tuple[Scenario, list[RunResult]]] = []
    for scenario in scenarios:
        if seed:
            scenario.config["seed"] = seed
        if keep_state:
            for key in ("seed", "seed_name", "seed-file", "seed_file"):
                scenario.config.pop(key, None)
        if runs is not None:
            scenario.config["runs"] = str(runs)
        if not quiet:
            _print_header(scenario, options.judge_model)
        all_results.append((scenario, _run_scenario(scenario, agent, options, quiet, explain)))

    if trace_out:
        Path(trace_out).write_text(
            json.dumps([_dump(r) for _, results in all_results for r in results],
                       indent=2, default=str),
            encoding="utf-8")
        if not quiet:
            console.print(f"[dim]Trace written to {trace_out}[/dim]")

    if as_json:
        click.echo(json.dumps(_summary(all_results), indent=2, default=str))
    elif len(all_results) > 1:
        _print_totals(all_results)

    sys.exit(_exit_code([r for _, results in all_results for r in results]))


def _passed(r: RunResult) -> bool:
    """Whether this run is a pass — one definition, used everywhere.

    A run with no criteria at all — `--task`, which exists to watch an agent
    rather than grade it — passes when the agent completed. Judging it against
    an empty criteria list would report 0/100 for a run that was never asking a
    question.
    """
    return not r.failed_must_pass and (not r.criteria or r.score == 100)


def _exit_code(results: list[RunResult]) -> int:
    """0 every run passed, 1 a criterion failed, 2 nothing could be scored."""
    if any(not r.scored for r in results):
        return 2
    return 0 if all(_passed(r) for r in results) else 1


# -- running ------------------------------------------------------------------


def _run_scenario(scenario, agent, options, quiet, explain) -> list[RunResult]:
    from checkpoint.engine import run_scenario

    results: list[RunResult] = []
    for i in range(scenario.runs):
        if scenario.runs > 1 and not quiet:
            console.print(f"[dim]run {i + 1}/{scenario.runs}[/dim]")
        started = time.perf_counter()
        result = run_scenario(scenario, agent, options=options)
        results.append(result)
        if not quiet:
            _print_result(result)
        _persist(result, scenario, options.judge_model,
                 duration_ms=(time.perf_counter() - started) * 1000, explain=explain)
    return results


def _load(path: Path) -> Scenario:
    scenario = parse_file(path)
    for problem in scenario.problems:
        console.print(f"[yellow]{plain(path.name)}: {plain(problem)}[/yellow]")
    return scenario


def _wanted(scenario: Scenario, tag: str | None, quiet: bool) -> bool:
    """Whether to run this file: scenarios only, matching the tag if one is given."""
    if not scenario.runnable:
        if not quiet:
            name = Path(scenario.source_path).name if scenario.source_path else "scenario"
            console.print(f"[dim]skipping {plain(name)}: no ## Task section[/dim]")
        return False
    return tag is None or tag in scenario.tags


def _inline_scenario(task: str, twins: str | None) -> Scenario:
    scenario = Scenario(title="inline task", prompt=task)
    scenario.config["twins"] = twins or "github"
    return scenario


def _streamer():
    def emit(stream: str, line: str) -> None:
        style = "dim" if stream == "stdout" else "yellow"
        console.print(f"[{style}]  {plain(line.rstrip())}[/{style}]", highlight=False)
    return emit


# -- output -------------------------------------------------------------------


def _print_header(scenario: Scenario, judge_model: str) -> None:
    title = scenario.title or (Path(scenario.source_path).stem if scenario.source_path
                               else "untitled")
    parts = [", ".join(scenario.twins) or "no twins"]
    if scenario.runs > 1:
        parts.append(f"{scenario.runs} runs")
    if any(c.kind == "P" for c in scenario.criteria):
        parts.append(f"judge {judge_model}")
    console.print()
    console.print(f"[bold]{plain(title)}[/bold]  [dim]{' · '.join(parts)}[/dim]")


def _print_result(r: RunResult) -> None:
    if r.error:
        console.print(f"  [red]{plain(r.error)}[/red]")
        if r.stderr.strip():
            tail = "\n".join(r.stderr.strip().splitlines()[-12:])
            console.print(f"[dim]{plain(tail)}[/dim]", highlight=False)
    for warning in r.warnings:
        console.print(f"  [yellow]note:[/yellow] {plain(warning)}")
    for problem in r.eval_errors:
        console.print(f"  [magenta]cannot score:[/magenta] {plain(problem)}")

    for c in r.criteria:
        label = f"[{c.kind}{'!' if c.must_pass else ''}]".ljust(4)
        console.print(f"  {criterion_mark(c)} [dim]{label}[/dim] {plain(c.text)}",
                      highlight=False)
        # Why it went that way is what you need when it failed, and noise when
        # it passed — except for a judged criterion, where the reasoning *is*
        # the evidence that the verdict was thought about.
        if c.status != "pass" or c.kind == "P":
            for line in _reasons(c):
                console.print(f"      [dim]{plain(line)}[/dim]", highlight=False)

    if r.criteria:
        console.print()

    # A run with no criteria was never asking a question — `--task` exists to
    # watch an agent work. Printing 0/100 would read as a failure.
    headline = (f"[{score_color(r.score)}]{r.score:.0f}/100[/{score_color(r.score)}]"
                if r.criteria else "[dim]nothing scored[/dim]")
    facts = [f"{len(r.trace)} API call{'s' if len(r.trace) != 1 else ''}",
             f"{r.duration_s:.1f}s"]
    # The score is over the criteria that could be scored, so say out loud when
    # that is not all of them. Without this the run reads as a clean pass and
    # then exits 2, and the terminal and the exit code tell different stories.
    unscored = [c for c in r.criteria if c.status == "error"]
    if unscored:
        facts.append(f"[yellow]{len(unscored)} not scored[/yellow]")
    if r.timed_out:
        facts.append("[red]timed out[/red]")
    elif not r.complete and not r.error:
        facts.append(f"[red]exit {r.exit_code}[/red]")
    blocked = sorted({e.get("host", "?") for e in r.egress if not e.get("allowed")})
    if blocked:
        # Naming a couple is enough to act on; the full list is in the record.
        shown = ", ".join(blocked[:2])
        more = f" +{len(blocked) - 2}" if len(blocked) > 2 else ""
        facts.append(f"[yellow]blocked {shown}{more}[/yellow]")
    console.print(f"  {headline}  [dim]{' · '.join(facts)}[/dim]")


def _reasons(criterion) -> list[str]:
    """The explanation under a criterion, one line per thing worth saying."""
    lines = [criterion.reasoning] if criterion.reasoning else []
    if criterion.assertion and criterion.status != "pass":
        lines.append(criterion.assertion)
    return lines


def _print_totals(all_results) -> None:
    scored = [r for _, results in all_results for r in results]
    passed = sum(1 for r in scored if _passed(r))
    color = "green" if passed == len(scored) else "red"
    console.print()
    console.print(f"[bold {color}]{passed}/{len(scored)} runs passed[/bold {color}]")
    for scenario, results in all_results:
        failed = [r for r in results if not _passed(r)]
        if failed:
            name = scenario.title or Path(scenario.source_path or "?").name
            console.print(f"  [red]{plain(name)}[/red] [dim]{len(failed)}/{len(results)} failed[/dim]")


def _summary(all_results) -> dict:
    scenarios = []
    for scenario, results in all_results:
        scores = [r.score for r in results]
        scenarios.append({
            "scenario": scenario.title or None,
            "path": scenario.source_path,
            "runs": len(results),
            "score_avg": sum(scores) / len(scores) if scores else 0.0,
            "score_min": min(scores, default=0.0),
            "passed": sum(1 for r in results if _passed(r)),
            "results": [_dump(r) for r in results],
        })
    every = [r for _, results in all_results for r in results]
    return {
        "scenarios": scenarios,
        "runs": len(every),
        "passed": sum(1 for r in every if _passed(r)),
        "unscored": sum(1 for r in every if not r.scored),
    }


def _dump(r: RunResult) -> dict:
    return {
        "run_id": r.run_id,
        "score": r.score,
        "complete": r.complete,
        "scored": r.scored,
        "exit_code": r.exit_code,
        "duration_s": round(r.duration_s, 3),
        "timed_out": r.timed_out,
        "error": r.error,
        "final_answer": r.final_answer,
        "criteria": [c.__dict__ for c in r.criteria],
        "trace": r.trace,
        "state": r.state,
        "views": r.views,
        "egress": r.egress,
        "warnings": r.warnings,
        "eval_errors": r.eval_errors,
    }


def _persist(r: RunResult, scenario: Scenario, judge_model: str, *,
             duration_ms: float, explain: bool) -> None:
    """Record the run so `checkpoint runs` and the dashboard can show it.

    Best effort: the score is already on screen, so a storage problem is a note,
    never an error.
    """
    from checkpoint.run_record import build_record, write_record

    analysis: dict[str, str] = {}
    failed = [c.text for c in r.criteria if not c.passed]
    if explain and failed and r.complete:
        from checkpoint.failure_analyzer import analyze
        try:
            analysis = analyze(failed, task=scenario.prompt, final_answer=r.final_answer,
                               trace=r.trace, state=r.state, model=judge_model)
        except Exception as e:  # noqa: BLE001 — an explanation is a bonus, not the result
            console.print(f"[dim]could not explain failures: {plain(e)}[/dim]")

    record = build_record(
        scenario_name=scenario.title or Path(scenario.source_path or "inline").stem,
        scenario_path=scenario.source_path,
        satisfaction=r.score,
        criteria=r.criteria,
        evaluator_model=judge_model,
        evaluator_model_source="config",
        final_answer=r.final_answer,
        stdout=r.stdout,
        stderr=r.stderr,
        trace=r.trace,
        state=r.state,
        error=r.error,
        exit_code=r.exit_code,
        failure_analysis=analysis or None,
        agent={"name": r.agent or "agent", "cmd": r.agent_command or r.agent},
        agent_trace=r.agent_trace or None,
        duration_ms=round(duration_ms, 1),
        run_id=r.run_id or None,
        warnings=r.warnings,
        egress=r.egress,
        twins=r.twins,
    )
    try:
        write_record(record)
    except OSError as e:
        console.print(f"[dim]could not save the run record: {plain(e)}[/dim]")
