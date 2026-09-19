"""Run the gate: execute each real scenario N times and summarize the distribution."""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..engine import Agent, RunOptions, Sandbox, SandboxError, run_scenario, scenario_twins
from ..llm import DEFAULT_MODEL
from ..runner import RunResult
from ..scenario import Scenario, parse_file
from .baseline import Baseline, criteria_hash, scenario_key
from .verdict import (
    EXIT_CODES,
    GatePolicy,
    GateResult,
    ScenarioStat,
    SkippedScenario,
    decide_verdict,
    error_scenario,
    summarize_scenario,
)

# progress(scenario_name, run_index, total_runs, score, complete)
ProgressFn = Callable[[str, int, int, float, bool], None]
# on_result(scenario_path, run_index, result) — e.g. to persist run records
ResultFn = Callable[[Path, int, RunResult], None]

#: Environment variable holding the API key for each judge provider. A judge we
#: cannot authenticate marks every `[P]` criterion failed, which would read as a
#: badly behaving agent; the gate refuses to run instead.
_JUDGE_KEY_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


@dataclass(frozen=True)
class LoadedScenario:
    """A file under the gate target that really is a scenario."""

    key: str      # path relative to the target, POSIX — unique within a run
    path: Path
    scenario: Scenario


@dataclass
class _Samples:
    """What N runs of one scenario produced."""

    scores: list[float] = field(default_factory=list)
    completes: list[bool] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    """Human-readable lines for every run that did not pass cleanly."""
    error_runs: int = 0
    """Runs that produced no sample at all (sandbox/judge/crash)."""
    error_reasons: list[str] = field(default_factory=list)


def collect_scenarios(target: Path) -> tuple[list[LoadedScenario], list[SkippedScenario]]:
    """Split the files under ``target`` into runnable scenarios and skips.

    The gate used to run every ``*.md`` below the target, so a ``README.md`` was
    "run" with an empty task, scored 0, and dragged the verdict down. A file is
    a scenario only when it gives the agent something to do and gives us
    something to score; anything else is reported as skipped, with the reason.
    """
    files = sorted(target.rglob("*.md")) if target.is_dir() else [target]
    loaded: list[LoadedScenario] = []
    skipped: list[SkippedScenario] = []
    for path in files:
        key = scenario_key(target, path)
        try:
            scenario = parse_file(path)
        except (OSError, ValueError) as e:
            skipped.append(SkippedScenario(key, f"cannot be read as a scenario — {e}"))
            continue
        if not scenario.prompt.strip():
            skipped.append(SkippedScenario(
                key, "no '## Prompt' (or '## Task') section — not a scenario"))
            continue
        if not scenario.criteria:
            skipped.append(SkippedScenario(
                key, "no '## Success Criteria' section — nothing to score the agent against"))
            continue
        loaded.append(LoadedScenario(key=key, path=path, scenario=scenario))
    return loaded, skipped


def judge_credential_error(judge_model: str, scenarios: Sequence[Scenario]) -> str | None:
    """Why the LLM judge cannot run, or None when it can (or is not needed).

    Checked before any run: without it a missing key turns every judged
    criterion into a failure and the gate reports a broken *agent* instead of a
    broken *setup* — after burning N runs to get there.
    """
    if not any(c.kind == "P" for s in scenarios for c in s.criteria):
        return None
    if os.environ.get("CHECKPOINT_LLM_BASE_URL"):
        return None  # an OpenAI-compatible endpoint supplies its own auth
    from ..llm import provider_for

    provider = provider_for(judge_model)
    keys = _JUDGE_KEY_ENV.get(provider)
    if keys is None:
        return None  # "compat"/local providers are configured by base URL alone
    if any(os.environ.get(k) for k in keys):
        return None
    return (
        f"the judge model {judge_model!r} needs {' or '.join(keys)}, which is not set — "
        "scenarios with [P] criteria cannot be scored"
    )


def run_gate(
    target: Path,
    harness_cmd: Sequence[str] | str | None,
    policy: GatePolicy,
    *,
    judge_model: str = DEFAULT_MODEL,
    progress: ProgressFn | None = None,
    baselines: Mapping[str, Baseline | float] | None = None,
    agent: Agent | None = None,
    options: RunOptions | None = None,
    concurrency: int = 1,
    on_result: ResultFn | None = None,
) -> GateResult:
    """Run every scenario under ``target`` ``policy.runs`` times and decide a verdict.

    Each scenario gets one sandbox per worker, reused across that worker's runs
    (twins are reset between runs), so N runs cost one sandbox boot, not N.

    Nothing here is allowed to turn broken plumbing into a pass/fail verdict: a
    target that matched no scenario, a judge we cannot authenticate, and a
    scenario whose every run died in the sandbox all come back as ERROR.
    """
    baselines = baselines or {}
    agent = agent or Agent(command=harness_cmd or ())
    opts = options or RunOptions(judge_model=judge_model)

    loaded, skipped = collect_scenarios(target)
    if not loaded:
        return _fatal(policy, skipped, [
            f"no runnable scenario found under {target} "
            f"({len(skipped)} file(s) skipped; see 'skipped' for why)"
        ])
    problem = judge_credential_error(judge_model, [ls.scenario for ls in loaded])
    if problem:
        return _fatal(policy, skipped, [f"judge unavailable: {problem}"])

    stats: list[ScenarioStat] = []
    errors: list[str] = []
    notes: list[str] = []
    never_ran: list[str] = []

    for item in loaded:
        fingerprint = criteria_hash(item.scenario)
        baseline_rate = _baseline_rate(baselines.get(item.key), fingerprint)
        if baselines.get(item.key) is not None and baseline_rate is None:
            notes.append(
                f"{item.key}: stored baseline ignored — the success criteria changed "
                "since it was recorded, so a drop would not be a regression"
            )
        samples = _run_scenario_n(item, agent, opts, policy.runs,
                                  max(1, concurrency), progress, on_result)
        errors.extend(samples.messages)
        if not samples.scores:
            # Every run died before producing a verdict. Reporting a pass rate
            # here would be fiction; small N would have called it "flaky".
            stats.append(error_scenario(item.key, policy, samples.error_reasons,
                                        error_runs=samples.error_runs,
                                        criteria_hash=fingerprint))
            continue
        # If every *executed* run failed to complete, this is broken plumbing on
        # the agent's side (bad harness command, missing dependency) rather than
        # an agent answering badly — say so instead of only reporting 0/N.
        if not any(samples.completes):
            never_ran.append(item.key)
        stats.append(summarize_scenario(
            item.key, samples.scores, samples.completes, policy,
            baseline_rate=baseline_rate,
            error_runs=samples.error_runs,
            error_reasons=samples.error_reasons,
            criteria_hash=fingerprint,
        ))

    if never_ran:
        errors.append(
            "harness never executed successfully for: " + ", ".join(never_ran)
            + " — refusing to report a pass/fail verdict for an agent that did not run"
        )
    verdict, exit_code = decide_verdict(stats, policy)
    return GateResult(verdict=verdict, scenarios=stats, policy=policy, exit_code=exit_code,
                      errors=errors, skipped=skipped, notes=notes)


def _fatal(policy: GatePolicy, skipped: list[SkippedScenario], errors: list[str]) -> GateResult:
    """An ERROR result for a gate that could not start: no verdict, non-zero exit."""
    return GateResult(verdict="ERROR", scenarios=[], policy=policy,
                      exit_code=EXIT_CODES["ERROR"], errors=errors, skipped=skipped)


def _baseline_rate(entry: Baseline | float | None, fingerprint: str) -> float | None:
    """The comparable pass rate from a stored baseline, or None.

    A bare float is accepted so callers (and tests) can pass rates directly; a
    :class:`Baseline` additionally carries the criteria it was measured against
    and is discarded when those have changed.
    """
    if entry is None:
        return None
    if isinstance(entry, (int, float)):
        return float(entry)
    return entry.pass_rate if entry.applies_to(fingerprint) else None


def _infrastructure_error(result: RunResult | None) -> str | None:
    """Why this run carries no verdict about the agent, or None if it does.

    A sandbox that would not start and a judge that could not be reached are
    *our* failures, not the agent's. Counting them as failed runs is how a gate
    reports a working agent as broken — and, with a baseline in play, how it
    manufactures a regression.
    """
    if result is None:
        return "no result"
    if getattr(result, "setup_error", False):
        return result.error or "sandbox setup failed"
    scoring_problems = getattr(result, "eval_errors", ()) or ()
    if scoring_problems:
        return "; ".join(scoring_problems)
    for criterion in getattr(result, "criteria", ()) or ():
        # A criterion the evaluator could not decide (an unreachable judge, an
        # assertion that cannot be evaluated) is our failure, not the agent's.
        if getattr(criterion, "status", "") == "error":
            return getattr(criterion, "reasoning", "") or "a criterion could not be evaluated"
    return None


def _run_scenario_n(
    item: LoadedScenario,
    agent: Agent,
    opts: RunOptions,
    runs: int,
    concurrency: int,
    progress: ProgressFn | None,
    on_result: ResultFn | None,
) -> _Samples:
    """Run one scenario ``runs`` times and collect its samples and errors."""
    name = item.key
    # Indexed by run so concurrent workers can write without ordering games;
    # a run with an infrastructure error contributes no sample at the end.
    scores: list[float] = [0.0] * runs
    completes: list[bool] = [False] * runs
    infra: list[str | None] = [None] * runs
    messages: list[str] = []
    lock = threading.Lock()
    done = 0

    try:
        twins = scenario_twins(item.scenario)
    except (SandboxError, KeyError) as e:
        return _Samples(error_runs=runs,
                        error_reasons=[f"cannot prepare twins — {e}"],
                        messages=[f"{name}: cannot prepare twins — {e}"])

    def record(i: int, result: RunResult | None, error: str | None) -> None:
        nonlocal done
        broken = _infrastructure_error(result) if error is None else error
        complete = result is not None and result.complete and broken is None
        score = result.score if complete and result is not None else 0.0
        with lock:
            scores[i], completes[i], infra[i] = score, complete, broken
            if broken is not None:
                messages.append(f"{name}: run {i + 1} could not be evaluated — {broken}")
            elif not complete:
                agent_error = (result.error if result is not None else None) or "no result"
                messages.append(f"{name}: run {i + 1} did not complete — {agent_error}")
            done += 1
            finished = done
        if result is not None and on_result is not None:
            on_result(item.path, i, result)
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
                    result = run_scenario(item.scenario, agent, sandbox=sandbox, options=opts)
                except Exception as e:  # noqa: BLE001 — one bad run must not abort the gate
                    record(i, None, f"raised {e!r}")
                    continue
                record(i, result, None)
        finally:
            sandbox.stop()

    workers = min(concurrency, runs)
    shards = [list(range(w, runs, workers)) for w in range(workers)]
    if workers == 1:
        worker(shards[0])
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"gate-{name}") as pool:
            list(pool.map(worker, shards))

    samples = _Samples(messages=messages)
    for i in range(runs):
        if infra[i] is None:
            samples.scores.append(scores[i])
            samples.completes.append(completes[i])
        else:
            samples.error_runs += 1
            if infra[i] not in samples.error_reasons:
                samples.error_reasons.append(infra[i])
    return samples
