"""``checkpoint gate`` — the verdict CI reads.

One run tells you almost nothing: agents are stochastic, and the run you happen
to watch is the one you believe. The gate runs every scenario N times and
decides from the distribution, so "it worked when I tried it" becomes a pass
rate with a confidence interval and an exit code.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from ._shared import (
    agent_options,
    console,
    fail,
    plain,
    project,
    resolve_agent,
    resolve_options,
    sandbox_options,
    short_path,
    verdict_color,
)

_CLASS_COLOR = {
    "stable_pass": "green", "stable_fail": "red", "regression": "red",
    "flaky": "yellow", "inconclusive": "yellow", "error": "magenta",
}


@click.command("gate")
@click.argument("target", required=False, type=click.Path(exists=True))
@agent_options
@sandbox_options
@click.option("-n", "--runs", type=int, default=None,
              help="Runs per scenario. [default: 16]")
@click.option("--pass-threshold", type=float, default=None, metavar="SCORE",
              help="Score out of 100 a single run needs to count as a pass. [default: 80]")
@click.option("--ship-min", type=float, default=None, metavar="RATE",
              help="Lower confidence bound (0-1) required to SHIP. [default: 0.80]")
@click.option("--block-max", type=float, default=None, metavar="RATE",
              help="Upper confidence bound (0-1) at or under which to BLOCK. [default: 0.50]")
@click.option("--confidence", type=float, default=None, metavar="LEVEL",
              help="Confidence level for the interval. [default: 0.95]")
@click.option("--regression-drop", type=float, default=None, metavar="RATE",
              help="Pass-rate drop against the baseline that reads as a regression. "
                   "[default: 0.20]")
@click.option("--allow-conditional", is_flag=True, default=False,
              help="Exit 0 on CONDITIONAL as well. Never covers BLOCK, INCONCLUSIVE or ERROR.")
@click.option("--report-only", is_flag=True, default=False,
              help="Print the verdict and exit 0 whatever it was. For adopting the "
                   "gate on an existing project before it blocks anything.")
@click.option("--strict", is_flag=True, default=False,
              help="Refuse CONDITIONAL even if checkpoint.toml allows it. Already the "
                   "default; use it to tighten a shared config from the command line.")
@click.option("--no-baseline", is_flag=True, default=False,
              help="Neither compare against nor update the stored pass rates.")
@click.option("--concurrency", "-j", type=int, default=None, metavar="N",
              help="Runs of a scenario to execute at once, each in its own "
                   "sandbox. [default: 1]")
@click.option("--model", default=None, metavar="MODEL",
              help="Judge model for [P] criteria.")
@click.option("--timeout", type=float, default=None, metavar="SECONDS",
              help="Kill the agent after this long, per run.")
@click.option("--name", default=None, metavar="NAME",
              help="Agent name recorded in the certificate. [default: the target's name]")
@click.option("--certificate", "cert_path", type=click.Path(dir_okay=False), default=None,
              metavar="PATH", help="Write a signed certificate of this verdict.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def gate(target, command, url, task_via, task_env, task_arg, cwd, intercept, egress,
         allow_hosts, rate_limit, read_only, runs, pass_threshold, ship_min, block_max,
         confidence, regression_drop, allow_conditional, report_only, strict, no_baseline,
         concurrency, model, timeout, name, cert_path, as_json):
    """Run every scenario N times and decide whether this build ships.

    TARGET is a scenario file or directory; with none, the project's scenarios.

    \b
    Verdict       Exit  Meaning
    SHIP           0    every scenario confidently passes
    BLOCK          1    a confident failure, a regression, or a scenario that
                        failed every run
    CONDITIONAL    2    enough runs to decide, results genuinely mixed
    INCONCLUSIVE   3    too few runs for SHIP to be reachable; the output says
                        how many it needs
    ERROR          4    the sandbox, judge or scenarios broke — no verdict is
                        possible, and none is invented

    Pass rates are remembered per scenario in .checkpoint/baselines.json and
    updated only on a SHIP, so a build that used to pass and now fails reads as
    a regression instead of quietly resetting the bar.
    """
    from checkpoint.gate import GatePolicy, baseline, default_concurrency, run_gate

    proj = project()
    root = _target(proj, target)
    agent = resolve_agent(proj, command, url=url, task_via=task_via, task_env=task_env,
                          task_arg=task_arg, cwd=cwd)
    options = resolve_options(
        proj, judge_model=model, timeout=timeout, intercept=intercept, egress=egress,
        allow_hosts=allow_hosts, rate_limit=rate_limit, read_only=read_only)

    try:
        policy = GatePolicy(
            runs=proj.gate_setting("runs", runs, 16),
            pass_threshold=proj.gate_setting("pass_threshold", pass_threshold, 80.0),
            confidence=proj.gate_setting("confidence", confidence, 0.95),
            ship_min=proj.gate_setting("ship_min", ship_min, 0.80),
            block_max=proj.gate_setting("block_max", block_max, 0.50),
            regression_drop=proj.gate_setting("regression_drop", regression_drop, 0.20),
            allow_conditional=allow_conditional or bool(proj.gate.get("allow_conditional")),
            strict=strict or bool(proj.gate.get("strict", False)),
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    # One id for the whole gate, minted before anything runs, so every run
    # record it produces and the certificate it may issue all point at each
    # other. A verdict you cannot open the failing runs of is a verdict you
    # have to take on faith.
    gate_id = uuid.uuid4().hex[:16]
    baselines = None if no_baseline else baseline.load(root)
    result = run_gate(
        root, None, policy,
        agent=agent, options=options, judge_model=options.judge_model,
        progress=None if as_json else _progress(policy.pass_threshold),
        baselines=baselines,
        concurrency=int(proj.gate_setting("concurrency", concurrency, default_concurrency())),
        on_result=_recorder(gate_id, options.judge_model),
    )

    updated: list[str] = []
    if not no_baseline and result.verdict == "SHIP":
        # Only a SHIP moves the bar. A gate that recorded flaky or failing rates
        # would ratchet itself downward: the run that should report a regression
        # writes its own degraded rate, and the next identical run sees no drop.
        updated = baseline.save(root, result.scenarios)

    certificate = None
    if cert_path:
        if result.verdict == "ERROR":
            # A certificate is evidence, and an ERROR gate produced none: the
            # sandbox, the judge or the scenarios broke before anything could be
            # measured. Signing that would hand a reviewer a document whose
            # signature verifies and whose contents attest to nothing, which is
            # worse than handing them nothing at all.
            console.print(
                "[yellow]No certificate written:[/yellow] the gate could not reach a "
                "verdict, so there is no evidence to certify. Fix the errors above "
                "and run it again.")
        else:
            certificate = _write_certificate(
                result, cert_path, agent_name=name or _subject_name(proj, root),
                command=agent.command, model=options.judge_model, gate_id=gate_id)

    record = _as_dict(result, policy, updated, certificate)
    record["gate_id"] = _record_verdict(record, target=str(root), gate_id=gate_id)

    record["report_only"] = report_only
    if as_json:
        click.echo(json.dumps(record, indent=2, default=str))
    else:
        _render(result, policy, updated, certificate)
    if report_only and result.exit_code != 0:
        # A gate that always exits 0 is indistinguishable from a passing one, so
        # the only safe version of this is the one that announces itself — every
        # time, and never settable from checkpoint.toml. It exists because the
        # alternative teams reach for is `|| true` in CI, which also swallows
        # ERROR: the case that means the gate itself broke.
        #
        # Not printed under --json, which promises one object and nothing else.
        # The record carries `report_only` and the true `exit_code` instead, so
        # nothing is hidden either way.
        if not as_json:
            console.print(
                f"[yellow]report-only:[/yellow] exiting 0. Without this flag, "
                f"{result.verdict} would exit {result.exit_code}.")
        sys.exit(0)
    sys.exit(result.exit_code)


def _recorder(gate_id: str, judge_model: str):
    """Persist every run the gate makes, stamped with the gate it belongs to.

    A gate that kept only its verdict left nothing to look at when it blocked:
    the sixteen runs behind the number were gone by the time anyone asked why.
    Best effort per run — a record that cannot be written is not worth failing
    a verdict over.
    """
    from checkpoint.run_record import build_record, write_record
    from checkpoint.scenario import parse_file

    def keep(path: Path, index: int, result) -> None:
        try:
            scenario = parse_file(path)
            entry = build_record(
                scenario_name=scenario.title or path.stem,
                scenario_path=str(path),
                satisfaction=result.score,
                criteria=result.criteria,
                evaluator_model=judge_model,
                evaluator_model_source="gate",
                final_answer=result.final_answer,
                stdout=result.stdout,
                stderr=result.stderr,
                trace=result.trace,
                state=result.state,
                error=result.error,
                exit_code=result.exit_code,
                run_id=result.run_id or None,
                agent={"name": result.agent or "agent", "cmd": result.agent_command or result.agent},
                agent_trace=result.agent_trace or None,
                duration_ms=round(result.duration_s * 1000, 1),
                warnings=result.warnings,
                egress=result.egress,
                twins=result.twins,
                gate_id=gate_id,
            )
            write_record(entry, pointer=False)
        except Exception as e:  # noqa: BLE001 — evidence is a bonus, the verdict is not
            console.print(f"[dim]could not save this run's evidence: {plain(e)}[/dim]")

    return keep


def _record_verdict(record: dict, *, target: str, gate_id: str | None = None) -> str | None:
    """Keep the verdict, so the dashboard can show what CI decided and why.

    Best effort: the verdict is already on screen and in the exit code, so a
    store that will not open is a dim note, never a different answer.
    """
    from checkpoint.store import SqliteRunStore

    gate_id = gate_id or uuid.uuid4().hex[:12]
    entry = {"gate_id": gate_id, "target": target,
             "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
    store = None
    try:
        store = SqliteRunStore()
        store.put_gate(entry)
        return gate_id
    except Exception as e:  # noqa: BLE001
        console.print(f"[dim]could not save this verdict: {plain(e)}[/dim]")
        return None
    finally:
        if store is not None:
            store.close()


def _target(proj, target) -> Path:
    if target:
        return Path(target)
    paths = proj.scenario_paths()
    if len(paths) > 1:
        fail("this project declares several scenario paths; gate one at a time",
             hint="  " + "\n  ".join(f"checkpoint gate {short_path(p)}" for p in paths))
    root = paths[0]
    if not root.exists():
        fail(f"no scenarios to gate: {short_path(root)} does not exist",
             hint="Run `checkpoint init` to set the project up, or name a target.")
    return root


def _progress(pass_threshold: float):
    def report(name: str, i: int, total: int, score: float, complete: bool) -> None:
        ok = complete and score >= pass_threshold
        color = "green" if ok else "red"
        console.print(f"[dim]{plain(name)}[/dim]  {i}/{total}  [{color}]{score:.0f}[/{color}]",
                      highlight=False)
    return report


# -- output -------------------------------------------------------------------


def _render(result, policy, updated, certificate) -> None:
    if not result.scenarios:
        # An empty table with headers reads as "nothing passed". Nothing ran.
        _render_outcome(result, updated, certificate)
        return
    k = min(policy.runs, 8)
    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Scenario", overflow="fold")
    table.add_column("Pass", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column(f"{policy.confidence:.0%} CI", justify="center")
    table.add_column(f"pass^{k}", justify="right")
    table.add_column("Reading")
    for s in result.scenarios:
        color = _CLASS_COLOR.get(s.classification, "white")
        table.add_row(
            plain(s.scenario),
            f"{s.passes}/{s.n}",
            f"{s.pass_rate:.0%}",
            f"[{s.ci.low:.0%}, {s.ci.high:.0%}]",
            f"{s.reliability(min(k, s.n)):.0%}",
            f"[{color}]{s.classification.replace('_', ' ')}[/{color}]",
        )
    console.print(table)

    for s in result.scenarios:
        console.print(f"  [dim]{plain(s.scenario)}: {plain(s.evidence())}[/dim]", highlight=False)
    _render_outcome(result, updated, certificate)


def _render_outcome(result, updated, certificate) -> None:
    """The verdict, and everything that qualifies it."""
    for skipped in result.skipped:
        console.print(f"[dim]skipped {plain(skipped.path)}: {plain(skipped.reason)}[/dim]")
    for note in result.notes:
        console.print(f"[dim]{plain(note)}[/dim]")
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} run error(s):[/yellow]")
        for message in result.errors[:10]:
            console.print(f"  [dim]{plain(message)}[/dim]")

    color = verdict_color(result.verdict)
    console.print(Panel.fit(
        f"[bold {color}]{result.verdict}[/bold {color}]  [dim]exit {result.exit_code}[/dim]",
        title="gate", border_style=color))
    if updated:
        console.print(f"[dim]Baseline updated for {len(updated)} scenario(s).[/dim]")
    if certificate:
        console.print(f"[dim]Signed certificate written to {plain(certificate)}.[/dim]")


def _as_dict(result, policy, updated, certificate) -> dict:
    return {
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "policy": {
            "runs": policy.runs, "pass_threshold": policy.pass_threshold,
            "confidence": policy.confidence, "ship_min": policy.ship_min,
            "block_max": policy.block_max, "strict": policy.strict,
            "regression_drop": policy.regression_drop,
            "allow_conditional": policy.allow_conditional,
            "runs_needed_to_ship": policy.min_runs_to_ship,
        },
        "scenarios": [{
            "scenario": s.scenario, "n": s.n, "passes": s.passes,
            "pass_rate": round(s.pass_rate, 4),
            "ci_low": round(s.ci.low, 4), "ci_high": round(s.ci.high, 4),
            # pass^k: the chance that k independent runs all pass.
            "pass_hat_k": {str(j): round(s.reliability(j), 4)
                           for j in (1, 2, 5, 10) if j <= s.n},
            "classification": s.classification,
            "mean_score": round(s.mean_score, 2),
            "runs_needed_to_ship": s.min_runs,
            "error_runs": s.error_runs,
            "errors": s.error_reasons,
            "baseline_rate": s.baseline_rate,
            "criteria_hash": s.criteria_hash,
            "evidence": s.evidence(),
        } for s in result.scenarios],
        "skipped": [{"path": s.path, "reason": s.reason} for s in result.skipped],
        "errors": result.errors,
        "notes": result.notes,
        "baseline_updated": updated,
        "certificate": certificate,
    }


def _subject_name(proj, root: Path) -> str:
    """What to call the agent on the certificate when nobody passed --name.

    The target is usually a directory of scenarios, so its own name says
    "scenarios" — which is the first field a reviewer reads and tells them
    nothing about what was tested. The project the gate ran in is a far better
    guess, and `--name` is there for when it is not.
    """
    return proj.root.name or root.stem


def _write_certificate(result, path, *, agent_name, command, model, gate_id) -> str:
    """Write the signed certificate for this gate run."""
    from checkpoint.gate.certificate import LocalSigner, build_certificate

    body = build_certificate(
        result, agent=agent_name,
        command=command,
        commit_sha=_commit_sha(), model=model, gate_id=gate_id)
    Path(path).write_text(json.dumps(LocalSigner().sign(body), indent=2), encoding="utf-8")
    return str(path)


def _commit_sha() -> str | None:
    """The commit this verdict is about, when there is one."""
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None
