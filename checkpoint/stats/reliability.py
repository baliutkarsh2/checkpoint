"""pass^k — how reliable is the agent if you run the scenario k times?

A pass *rate* answers "what fraction of runs pass?". Buyers of a release gate ask
a sharper question: "if this ships and runs k times in production, what's the
chance all k succeed?" That is pass^k. We report it because a 90%-pass agent has
only a ~35% chance that 10 consecutive runs all pass (pass^10 ~= 0.35) — i.e. a
~65% chance of at least one failure. The gate should make that visible, not hide
it behind a reassuring mean.

We use the unbiased tau-bench estimator (Sierra, tau-bench 2024):

    pass^k = C(passes, k) / C(n, k)

i.e. the probability that k trials drawn without replacement from the n observed
are all successes. This is an unbiased estimate of the true pass^k and, unlike
`(passes/n) ** k`, correctly returns 0 once fewer than k successes were seen.
"""
from __future__ import annotations

import math


def pass_hat_k(passes: int, n: int, k: int) -> float:
    """Unbiased estimate of pass^k from `passes` successes out of `n` trials.

    Returns a probability in [0, 1]. `k <= 0` is 1.0 (vacuously); `k > n` or
    fewer than `k` successes is 0.0.
    """
    if k <= 0:
        return 1.0
    if n <= 0 or k > n or passes < k:
        return 0.0
    return math.comb(passes, k) / math.comb(n, k)
