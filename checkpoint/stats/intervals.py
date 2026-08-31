"""Confidence intervals and stability classification for pass rates.

An agent is non-deterministic: the same build can pass a scenario on one run and
fail it on the next. A single green run is therefore a coin flip, not a verdict.
We run each scenario N times and reason about the *distribution* of outcomes with
a Wilson score interval — the standard small-sample interval for a binomial
proportion, which (unlike the naive normal approximation) stays inside [0, 1] and
behaves sensibly at 0/N and N/N.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# z for common two-sided confidence levels.
_Z = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}


def _z_for(confidence: float) -> float:
    if confidence in _Z:
        return _Z[confidence]
    # Fall back to the nearest tabulated level rather than importing scipy.
    return _Z[min(_Z, key=lambda c: abs(c - confidence))]


@dataclass(frozen=True)
class ProportionCI:
    passes: int
    n: int
    confidence: float
    point: float   # observed pass rate x/n
    low: float     # lower bound of the interval
    high: float    # upper bound

    @property
    def width(self) -> float:
        return self.high - self.low


def wilson_interval(passes: int, n: int, confidence: float = 0.95) -> ProportionCI:
    """Wilson score interval for `passes` successes out of `n` trials."""
    if n <= 0:
        return ProportionCI(0, 0, confidence, 0.0, 0.0, 1.0)
    if passes < 0 or passes > n:
        raise ValueError(f"passes={passes} out of range for n={n}")
    z = _z_for(confidence)
    p = passes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return ProportionCI(passes, n, confidence, p, low, high)


Stability = Literal["stable_pass", "stable_fail", "flaky", "regression"]


def classify_stability(
    ci: ProportionCI,
    *,
    ship_min: float = 0.95,
    block_max: float = 0.50,
    baseline_rate: float | None = None,
    regression_drop: float = 0.20,
) -> Stability:
    """Classify a scenario's pass-rate CI into a gate-relevant verdict.

    - ``stable_pass``  — we're confident the pass rate is high (CI low >= ship_min).
    - ``stable_fail``  — we're confident it's low (CI high <= block_max).
    - ``regression``   — a real drop vs. a known baseline (point estimate fell by
                         at least ``regression_drop``). Overrides a non-passing
                         base verdict so a build that *used* to pass and now
                         fails reads as a regression, not just a failure.
    - ``flaky``        — everything else: the interval straddles the thresholds,
                         so more runs are needed before trusting either verdict.
    """
    if ci.low >= ship_min:
        base = "stable_pass"
    elif ci.high <= block_max:
        base = "stable_fail"
    else:
        base = "flaky"

    if base != "stable_pass" and baseline_rate is not None and (baseline_rate - ci.point) >= regression_drop:
        return "regression"
    return base
