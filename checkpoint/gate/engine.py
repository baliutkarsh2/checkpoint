"""Run the gate: execute each scenario N times and summarize the distribution."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from ..runner import run_once
from ..scenario import parse_file
from .verdict import GatePolicy, GateResult, ScenarioStat, decide_verdict, summarize_scenario

# progress(scenario_name, run_index, total_runs, score, complete)
ProgressFn = Callable[[str, int, int, float, bool], None]


def _collect_scenarios(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*.md"))
    return [target]


def run_gate(
    target: Path,
    harness_cmd: list[str],
    policy: GatePolicy,
    *,
    judge_model: str = "gpt-4o-mini",
    progress: ProgressFn | None = None,
    baselines: dict[str, float] | None = None,
) -> GateResult:
    """Run every scenario under `target` `policy.runs` times and decide a verdict."""
    baselines = baselines or {}
    stats: list[ScenarioStat] = []
    errors: list[str] = []

    for path in _collect_scenarios(target):
        name = path.name
        scores: list[float] = []
        completes: list[bool] = []
        for i in range(policy.runs):
            try:
                result = run_once(parse_file(path), harness_cmd, judge_model=judge_model)
            except Exception as e:  # noqa: BLE001 — one bad run must not abort the gate
                errors.append(f"{name}: run {i + 1} raised {e!r}")
                scores.append(0.0)
                completes.append(False)
                if progress:
                    progress(name, i + 1, policy.runs, 0.0, False)
                continue
            complete = bool(result.complete) and not result.error
            score = result.score if complete else 0.0
            scores.append(score)
            completes.append(complete)
            if progress:
                progress(name, i + 1, policy.runs, score, complete)

        stats.append(
            summarize_scenario(name, scores, completes, policy, baseline_rate=baselines.get(name))
        )

    verdict, exit_code = decide_verdict(stats, policy)
    return GateResult(verdict=verdict, scenarios=stats, policy=policy, exit_code=exit_code, errors=errors)
