"""Confidence intervals and stability classification for pass rates.

An agent is non-deterministic: the same build can pass a scenario on one run and
fail it on the next. A single green run is therefore a coin flip, not a verdict.
We run each scenario N times and reason about the *distribution* of outcomes with
a Wilson score interval — the standard small-sample interval for a binomial
proportion, which (unlike the naive normal approximation) stays inside [0, 1] and
behaves sensibly at 0/N and N/N.

Two properties matter more than elegance here, because getting them wrong lets a
bad agent through:

* the interval must reflect the confidence level the user *asked for* (see
  :func:`z_for`), and
* "the runs do not support a decision" must be distinguishable from "the runs
  support a middling result" (see :func:`runs_needed` and
  :func:`classify_stability`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

# Slack for every threshold comparison in this module. Pass rates are ratios of
# small integers, so a drop that is exactly 0.20 on paper routinely materializes
# as 0.19999999999999996 — and a bare `>=` would silently miss the regression the
# comparison was written to catch.
TOLERANCE = 1e-9


def z_for(confidence: float) -> float:
    """Two-sided normal critical value for ``confidence``.

    Computed exactly from the normal quantile function rather than looked up in
    a table: the table only held a handful of levels and snapped everything else
    to the nearest entry, so ``--confidence 0.85`` silently used the z for 0.80.
    A user asking for a *wider* interval got a narrower one — weaker evidence
    than they asked for, in the direction that ships.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence!r}")
    return NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)


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
    z = z_for(confidence)
    p = passes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return ProportionCI(passes, n, confidence, p, low, high)


def runs_needed(ship_min: float, confidence: float = 0.95) -> int:
    """Smallest N at which a *flawless* scenario can clear ``ship_min``.

    For a perfect n-of-n the Wilson lower bound collapses to ``n / (n + z**2)``,
    so the smallest N that clears ``ship_min`` is

        N = ceil( ship_min * z**2 / (1 - ship_min) )

    — 16 runs at the default ``ship_min=0.80`` and 95% confidence. Below that,
    *no* number of consecutive passes can SHIP, which is why the gate reports
    this figure instead of labelling an underpowered run "flaky" and waving it
    through: at ``-n 5`` a perfect 5/5 is not a weak pass, it is no evidence.
    """
    if not 0.0 <= ship_min < 1.0:
        raise ValueError(
            f"ship_min must be in [0, 1), got {ship_min!r} — a pass rate of 1.0 "
            "cannot be proven by any finite number of runs"
        )
    z2 = z_for(confidence) ** 2
    n = max(1, math.ceil(ship_min * z2 / (1.0 - ship_min) - TOLERANCE))
    # The closed form is exact, but re-check it against the interval itself so
    # the number we print can never disagree with the bound we gate on.
    while wilson_interval(n, n, confidence).low < ship_min - TOLERANCE:
        n += 1
    return n


Stability = Literal[
    "stable_pass", "stable_fail", "flaky", "inconclusive", "regression", "error"
]


def is_regression(
    ci: ProportionCI,
    baseline_rate: float | None,
    regression_drop: float = 0.20,
) -> bool:
    """True when the runs show a real drop against a recorded baseline.

    Two conditions, both required:

    * the point estimate fell by at least ``regression_drop`` (tolerance-safe,
      so an exactly-0.20 drop that floats to 0.1999… still counts), and
    * the baseline sits *above* the whole current interval — the runs we just
      saw are inconsistent, at this confidence level, with the agent still
      performing at its baseline rate.

    The second condition is the significance test. Comparing point estimates
    alone turns three unlucky runs into a "regression" and blocks a release on
    noise; requiring the baseline to fall outside the interval means we only
    blame the build when the evidence can carry it.
    """
    if baseline_rate is None or ci.n <= 0:
        return False
    if (baseline_rate - ci.point) < regression_drop - TOLERANCE:
        return False
    return ci.high < baseline_rate - TOLERANCE


def classify_stability(
    ci: ProportionCI,
    *,
    ship_min: float = 0.95,
    block_max: float = 0.50,
    baseline_rate: float | None = None,
    regression_drop: float = 0.20,
    min_runs: int | None = None,
) -> Stability:
    """Classify a scenario's pass-rate CI into a gate-relevant verdict.

    - ``error``        — no usable sample at all (every run failed to produce
                         one). Carries no pass/fail information.
    - ``regression``   — a statistically meaningful drop vs. a known baseline
                         (see :func:`is_regression`). Checked first: a build
                         that used to pass and now does not should read as a
                         regression whatever else the interval says.
    - ``stable_pass``  — we're confident the pass rate is high (CI low >= ship_min).
    - ``stable_fail``  — we're confident it's low (CI high <= block_max), or
                         *every* run failed. Zero passes is a decision no matter
                         how few runs there were: an agent that never once
                         succeeded has not earned a soft verdict.
    - ``inconclusive`` — the run was underpowered: fewer than ``min_runs``
                         (default: :func:`runs_needed`) samples, so even a
                         flawless result could not have cleared ``ship_min``.
    - ``flaky``        — enough runs to have decided, and the answer is genuinely
                         in between: the interval straddles the thresholds.
    """
    if ci.n <= 0:
        return "error"
    if is_regression(ci, baseline_rate, regression_drop):
        return "regression"
    if ci.low >= ship_min - TOLERANCE:
        return "stable_pass"
    if ci.high <= block_max + TOLERANCE or ci.passes == 0:
        return "stable_fail"
    needed = runs_needed(ship_min, ci.confidence) if min_runs is None else min_runs
    if ci.n < needed:
        return "inconclusive"
    return "flaky"
