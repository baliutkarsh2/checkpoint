"""Run the gate: execute each scenario N times and summarize the distribution."""
from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..engine import Agent, RunOptions, Sandbox, SandboxError, run_scenario, scenario_twins
from ..runner import RunResult
from ..scenario import parse_file
from .verdict import (
    GatePolicy,
    GateResult,
    ScenarioStat,
    decide_verdict,
    summarize_scenario,
)

# progress(scenario_name, run_index, total_runs, score, complete)
ProgressFn = Callable[[str, int, int, float, bool], None]
# on_result(scenario_path, run_index, result) — e.g. to persist run records
ResultFn = Callable[[Path, int, RunResult], None]


def _collect_scenarios(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*.md"))
    return [target]


def run_gate(
    target: Path,
    harness_cmd: Sequence[str] | str | None,
    policy: GatePolicy,
    *,
    judge_model: str = "gpt-4o-mini",
    progress: ProgressFn | None = None,
    baselines: dict[str, float] | None = None,
    agent: Agent | None = None,
    options: RunOptions | None = None,
    concurrency: int = 1,
    on_result: ResultFn | None = None,
) -> GateResult:
    """Run every scenario under ``target`` ``policy.runs`` times and decide a verdict.

    Each scenario gets one sandbox per worker, reused across that worker's runs
    (twins are reset between runs), so N runs cost one sandbox boot, not N.
    """
    baselines = baselines or {}
    agent = agent or Agent(command=harness_cmd or ())
    opts = options or RunOptions(judge_model=judge_model)
    stats: list[ScenarioStat] = []
    errors: list[str] = []
    never_ran: list[str] = []

    for path in _collect_scenarios(target):
        name = path.name
        scores, completes, errs = _run_scenario_n(path, agent, opts, policy.runs,
                                                  max(1, concurrency), progress, on_result)
        errors.extend(errs)
        # If EVERY run of a scenario failed to execute, this is broken plumbing
        # (bad harness command, missing dependency), not a statistical result.
        # Small N would otherwise classify it "flaky" -> CONDITIONAL -> exit 0,
        # i.e. a green build for an agent that was never actually tested.
        if scores and not any(completes):
            never_ran.append(name)
        stats.append(
            summarize_scenario(name, scores, completes, policy, baseline_rate=baselines.get(name))
        )

    verdict, exit_code = decide_verdict(stats, policy)
    if never_ran:
        errors.append(
            "harness never executed successfully for: " + ", ".join(never_ran)
            + " — refusing to report a pass/fail verdict for an agent that did not run"
        )
        verdict, exit_code = "BLOCK", 1
    return GateResult(verdict=verdict, scenarios=stats, policy=policy, exit_code=exit_code, errors=errors)


def _run_scenario_n(
    path: Path,
    agent: Agent,
    opts: RunOptions,
    runs: int,
    concurrency: int,
    progress: ProgressFn | None,
    on_result: ResultFn | None,
) -> tuple[list[float], list[bool], list[str]]:
    """Run one scenario ``runs`` times; return per-run scores, completion flags, errors."""
    name = path.name
    scores: list[float] = [0.0] * runs
    completes: list[bool] = [False] * runs
    errors: list[str] = []
    lock = threading.Lock()
    done = 0

    try:
        scenario = parse_file(path)
        twins = scenario_twins(scenario)
    except (OSError, KeyError, ValueError) as e:
        return scores, completes, [f"{name}: cannot load scenario — {e}"]

    def record(i: int, result: RunResult | None, error: str | None) -> None:
        nonlocal done
        complete = result is not None and result.complete
        score = result.score if complete and result is not None else 0.0
        with lock:
            scores[i], completes[i] = score, complete
            if error:
                errors.append(f"{name}: run {i + 1} did not complete — {error}")
            done += 1
            finished = done
        if result is not None and on_result is not None:
            on_result(path, i, result)
        if progress:
            progress(name, finished, runs, score, complete)

    def worker(indices: list[int]) -> None:
        try:
            sandbox = Sandbox(twins, intercept=opts.intercept, egress=opts.egress,
                              allow_hosts=opts.allow_hosts)
            sandbox.start()
        except (SandboxError, KeyError) as e:
            for i in indices:
                record(i, None, f"sandbox setup failed: {e}")
            return
        try:
            for i in indices:
                try:
                    result = run_scenario(scenario, agent, sandbox=sandbox, options=opts)
                except Exception as e:  # noqa: BLE001 — one bad run must not abort the gate
                    record(i, None, f"raised {e!r}")
                    continue
                # A run that never completed is an execution failure, not an agent
                # answering badly: surface it instead of silently scoring it 0.
                record(i, result, None if result.complete else (result.error or "no result"))
        finally:
            sandbox.stop()

    workers = min(concurrency, runs)
    shards = [list(range(w, runs, workers)) for w in range(workers)]
    if workers == 1:
        worker(shards[0])
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"gate-{name}") as pool:
            list(pool.map(worker, shards))
    return scores, completes, errors
