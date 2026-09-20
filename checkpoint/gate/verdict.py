"""Gate verdict model and the aggregation rule that turns per-scenario
pass-rate distributions into one release decision.

The policy is safe by default: **only SHIP exits 0**. Every other outcome —
including "the evidence cannot decide" — fails the build, because the failure
mode this product cannot afford is a green pipeline for an agent that was never
shown to work. A team that wants to ship on a middling result opts in with
``allow_conditional``; nobody opts into shipping on no evidence at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..stats import ProportionCI, classify_stability, runs_needed, wilson_interval

Verdict = Literal["SHIP", "CONDITIONAL", "INCONCLUSIVE", "BLOCK", "ERROR"]

#: Exit code per verdict. Distinct codes let CI tell "blocked" from "could not
#: decide" from "the harness is broken" without parsing JSON; every one of them
#: except SHIP is non-zero.
EXIT_CODES: dict[str, int] = {
    "SHIP": 0,          # every scenario confidently passes
    "BLOCK": 1,         # a confident failure, a regression, or an all-failed scenario
    "CONDITIONAL": 2,   # enough runs to decide, and the answer is "in between"
    "INCONCLUSIVE": 3,  # too few runs to decide anything — run more
    "ERROR": 4,         # sandbox/judge/scenario plumbing broke; no verdict is possible
}


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
    allow_conditional: bool = False
    """Treat CONDITIONAL as success (exit 0). The only supported way to make a
    non-SHIP verdict green, and it never covers INCONCLUSIVE, BLOCK or ERROR."""
    strict: bool = False
    """Refuse CONDITIONAL even when ``allow_conditional`` is set. This is already
    the default; the flag exists so a release pipeline can override a shared
    config that opted into conditional passes."""

    def __post_init__(self) -> None:
        # A misconfigured policy must fail loudly at construction: silently
        # clamping it would produce a verdict nobody asked for.
        if self.runs < 1:
            raise ValueError(f"runs must be at least 1, got {self.runs}")
        if not 0.0 <= self.pass_threshold <= 100.0:
            raise ValueError(f"pass_threshold must be in [0, 100], got {self.pass_threshold}")
        if not 0.0 < self.confidence < 1.0:
            raise ValueError(f"confidence must be in (0, 1), got {self.confidence}")
        if not 0.0 <= self.ship_min < 1.0:
            raise ValueError(
                f"ship_min must be in [0, 1), got {self.ship_min} — a pass rate of "
                "1.0 cannot be proven by any finite number of runs"
            )
        if not 0.0 <= self.block_max <= 1.0:
            raise ValueError(f"block_max must be in [0, 1], got {self.block_max}")
        if self.block_max >= self.ship_min:
            raise ValueError(
                f"block_max ({self.block_max}) must be below ship_min ({self.ship_min}); "
                "otherwise a scenario could be a confident pass and a confident fail at once"
            )
        if not 0.0 <= self.regression_drop <= 1.0:
            raise ValueError(f"regression_drop must be in [0, 1], got {self.regression_drop}")

    @property
    def min_runs_to_ship(self) -> int:
        """Runs a flawless scenario needs before it *can* SHIP under this policy."""
        return runs_needed(self.ship_min, self.confidence)

    def conditional_is_success(self) -> bool:
        return self.allow_conditional and not self.strict


@dataclass(frozen=True)
class SkippedScenario:
    """A file under the gate target that was not run, and why."""

    path: str
    reason: str


@dataclass
class ScenarioStat:
    scenario: str
    """Path relative to the gate target, e.g. ``github/smoke.md`` — unique
    within a run, unlike the bare file name."""
    n: int
    """Runs that produced a pass/fail sample. Excludes infrastructure errors."""
    passes: int
    scores: list[float]
    ci: ProportionCI
    classification: str
    mean_score: float
    baseline_rate: float | None = None
    min_runs: int = 0
    """Runs this scenario would need before SHIP is reachable at all."""
    error_runs: int = 0
    """Runs that produced no sample because the plumbing failed (sandbox,
    judge credential, crash). They are never counted as agent failures."""
    error_reasons: list[str] = field(default_factory=list)
    criteria_hash: str = ""
    """Fingerprint of the scenario's success criteria, so a stored baseline can
    be invalidated when the thing being measured changes."""
    ship_min: float = 0.0
    """The policy's SHIP threshold, carried here so a stat can explain itself
    without the caller re-plumbing the policy."""

    @property
    def pass_rate(self) -> float:
        return self.ci.point

    def reliability(self, k: int) -> float:
        """pass^k — unbiased estimate that k independent runs all pass."""
        from checkpoint.stats.reliability import pass_hat_k
        return pass_hat_k(self.passes, self.n, k)

    def evidence(self) -> str:
        """One line stating what these runs support — and, when they cannot
        decide, exactly how many runs would."""
        if self.classification == "error":
            reason = self.error_reasons[0] if self.error_reasons else "no run produced a result"
            return f"no usable runs ({self.error_runs} errored): {reason}"
        interval = f"{int(self.ci.confidence * 100)}% CI [{self.ci.low:.2f}, {self.ci.high:.2f}]"
        body = f"{self.passes}/{self.n} runs passed, {interval}"
        if self.error_runs:
            body += f" ({self.error_runs} run(s) errored and were excluded)"
        if self.classification == "inconclusive":
            return (
                f"{body} — cannot decide at n={self.n}: SHIP needs >= {self.min_runs} "
                f"clean runs at ship_min {self.ship_min:.2f}"
            )
        if self.classification == "regression":
            base = self.baseline_rate if self.baseline_rate is not None else 0.0
            return f"{body} — regression: baseline was {base:.2f}"
        return body


@dataclass
class GateResult:
    verdict: Verdict
    scenarios: list[ScenarioStat]
    policy: GatePolicy
    exit_code: int
    errors: list[str] = field(default_factory=list)
    skipped: list[SkippedScenario] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Informational lines that are not failures, e.g. a baseline discarded
    because the scenario's criteria changed."""


def summarize_scenario(
    scenario: str,
    scores: list[float],
    completes: list[bool],
    policy: GatePolicy,
    baseline_rate: float | None = None,
    *,
    error_runs: int = 0,
    error_reasons: list[str] | None = None,
    criteria_hash: str = "",
) -> ScenarioStat:
    """Summarize one scenario's samples into a classified statistic.

    ``scores``/``completes`` hold only runs that produced a pass/fail sample;
    runs that died for infrastructure reasons are counted in ``error_runs`` and
    deliberately kept out of the denominator — a sandbox that would not start
    says nothing about the agent.
    """
    n = len(scores)
    # An incomplete run (agent crash / timeout) counts as a failure — you can't
    # ship on a run that never produced a verdict.
    passes = sum(
        1 for s, ok in zip(scores, completes, strict=False) if ok and s >= policy.pass_threshold
    )
    ci = wilson_interval(passes, n, policy.confidence)
    min_runs = policy.min_runs_to_ship
    classification = classify_stability(
        ci,
        ship_min=policy.ship_min,
        block_max=policy.block_max,
        baseline_rate=baseline_rate,
        regression_drop=policy.regression_drop,
        min_runs=min_runs,
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
        min_runs=min_runs,
        error_runs=error_runs,
        error_reasons=list(error_reasons or []),
        criteria_hash=criteria_hash,
        ship_min=policy.ship_min,
    )


def error_scenario(
    scenario: str,
    policy: GatePolicy,
    reasons: list[str],
    *,
    error_runs: int = 0,
    criteria_hash: str = "",
) -> ScenarioStat:
    """A scenario that produced no sample at all: classified ``error``, never
    pass/fail. Used when every run died in the sandbox or the judge."""
    return ScenarioStat(
        scenario=scenario,
        n=0,
        passes=0,
        scores=[],
        ci=wilson_interval(0, 0, policy.confidence),
        classification="error",
        mean_score=0.0,
        min_runs=policy.min_runs_to_ship,
        error_runs=error_runs,
        error_reasons=list(reasons),
        criteria_hash=criteria_hash,
        ship_min=policy.ship_min,
    )


def decide_verdict(stats: list[ScenarioStat], policy: GatePolicy) -> tuple[Verdict, int]:
    """Aggregate per-scenario classifications into one verdict + exit code.

    Worst outcome wins:

    - **BLOCK** — any scenario is a confident failure, a regression, or failed
      every single run.
    - **ERROR** — any scenario produced no usable sample (sandbox, judge, or
      scenario plumbing). No pass/fail verdict can be honest about it.
    - **INCONCLUSIVE** — some scenario ran too few times for SHIP to be
      reachable. Not a soft pass: the gate says how many runs it needs.
    - **CONDITIONAL** — enough runs, genuinely mixed results. Exits non-zero
      unless ``allow_conditional`` is set (and ``strict`` is not).
    - **SHIP** — every scenario is a confident pass.

    An empty list is ERROR, not SHIP: gating nothing is a broken invocation.
    """
    if not stats:
        return "ERROR", EXIT_CODES["ERROR"]
    classes = {s.classification for s in stats}
    verdict: Verdict
    if classes & {"stable_fail", "regression"}:
        verdict = "BLOCK"
    elif "error" in classes:
        verdict = "ERROR"
    elif "inconclusive" in classes:
        verdict = "INCONCLUSIVE"
    elif classes == {"stable_pass"}:
        verdict = "SHIP"
    else:
        verdict = "CONDITIONAL"
    return verdict, exit_code_for(verdict, policy)


def exit_code_for(verdict: Verdict, policy: GatePolicy) -> int:
    """Exit code for a verdict under a policy. Only SHIP is 0 by default."""
    if verdict == "CONDITIONAL" and policy.conditional_is_success():
        return 0
    return EXIT_CODES[verdict]


__all__ = [
    "EXIT_CODES",
    "GatePolicy",
    "GateResult",
    "ScenarioStat",
    "SkippedScenario",
    "Verdict",
    "decide_verdict",
    "error_scenario",
    "exit_code_for",
    "summarize_scenario",
]
