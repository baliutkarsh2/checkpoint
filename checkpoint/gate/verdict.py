"""Gate verdict model and the aggregation rule that turns per-scenario
pass-rate distributions into one ship/block decision."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..stats import ProportionCI, classify_stability, wilson_interval

Verdict = Literal["SHIP", "CONDITIONAL", "BLOCK"]


@dataclass
class GatePolicy:
    runs: int = 20
    pass_threshold: float = 80.0     # a single run "passes" at this score (0-100)
    confidence: float = 0.95
    # SHIP only when we're `confidence`-sure the true pass rate is at least
    # `ship_min`. With 20 runs the Wilson lower bound for a perfect 20/20 is
    # ~0.84, so 0.80 lets a clean build ship while a single failure in 20
    # (pass rate 0.95, CI low ~0.76) still lands CONDITIONAL — which is the
    # point: one flake in twenty is a real 5%-of-users failure mode.
    ship_min: float = 0.80           # CI lower bound must clear this to SHIP
    block_max: float = 0.50          # CI upper bound at/under this is a hard fail
    regression_drop: float = 0.20    # pass-rate drop vs baseline that flags a regression
    strict: bool = False             # if True, CONDITIONAL exits non-zero


@dataclass
class ScenarioStat:
    scenario: str
    n: int
    passes: int
    scores: list[float]
    ci: ProportionCI
    classification: str
    mean_score: float
    baseline_rate: float | None = None

    @property
    def pass_rate(self) -> float:
        return self.ci.point


@dataclass
class GateResult:
    verdict: Verdict
    scenarios: list[ScenarioStat]
    policy: GatePolicy
    exit_code: int
    errors: list[str] = field(default_factory=list)


def summarize_scenario(
    scenario: str,
    scores: list[float],
    completes: list[bool],
    policy: GatePolicy,
    baseline_rate: float | None = None,
) -> ScenarioStat:
    n = len(scores)
    # An incomplete run (harness crash / timeout) counts as a failure — you can't
    # ship on a run that never produced a verdict.
    passes = sum(
        1 for s, ok in zip(scores, completes) if ok and s >= policy.pass_threshold
    )
    ci = wilson_interval(passes, n, policy.confidence)
    classification = classify_stability(
        ci,
        ship_min=policy.ship_min,
        block_max=policy.block_max,
        baseline_rate=baseline_rate,
        regression_drop=policy.regression_drop,
    )
    mean = sum(scores) / n if n else 0.0
    return ScenarioStat(
        scenario=scenario,
        n=n,
        passes=passes,
        scores=scores,
        ci=ci,
        classification=classification,
        mean_score=mean,
        baseline_rate=baseline_rate,
    )


def decide_verdict(stats: list[ScenarioStat], policy: GatePolicy) -> tuple[Verdict, int]:
    """Aggregate per-scenario classifications into one verdict + exit code.

    - BLOCK if any scenario is a stable failure or a regression.
    - SHIP if every scenario is a stable pass.
    - CONDITIONAL otherwise (something is flaky — needs more runs or a look).
    """
    if not stats:
        return "BLOCK", 1
    classes = {s.classification for s in stats}
    if "stable_fail" in classes or "regression" in classes:
        return "BLOCK", 1
    if classes == {"stable_pass"}:
        return "SHIP", 0
    return "CONDITIONAL", (1 if policy.strict else 0)
