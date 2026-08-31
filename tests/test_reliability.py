"""pass^k reliability estimator + its surfacing in the gate."""
from __future__ import annotations

import math

import pytest

from checkpoint.stats import pass_hat_k
from checkpoint.gate.verdict import GatePolicy, summarize_scenario


def test_pass_hat_k_edges():
    assert pass_hat_k(10, 10, 1) == 1.0          # all passed -> any single run passes
    assert pass_hat_k(10, 10, 5) == 1.0          # ...and any 5 do too
    assert pass_hat_k(0, 10, 1) == 0.0           # none passed
    assert pass_hat_k(5, 10, 0) == 1.0           # k=0 is vacuously certain
    assert pass_hat_k(3, 10, 5) == 0.0           # fewer successes than k
    assert pass_hat_k(5, 10, 11) == 0.0          # k > n


def test_pass_hat_k_matches_hypergeometric():
    # 9/10 passed: chance a single random run passes is 9/10.
    assert pass_hat_k(9, 10, 1) == pytest.approx(0.9)
    # chance two random (without replacement) both pass = C(9,2)/C(10,2) = 36/45.
    assert pass_hat_k(9, 10, 2) == pytest.approx(math.comb(9, 2) / math.comb(10, 2))
    assert pass_hat_k(5, 10, 2) == pytest.approx(math.comb(5, 2) / math.comb(10, 2))


def test_pass_hat_k_monotonic_decreasing_in_k():
    vals = [pass_hat_k(8, 10, k) for k in range(1, 9)]
    assert all(a >= b for a, b in zip(vals, vals[1:]))


def test_scenario_stat_reliability_surfaced():
    policy = GatePolicy(runs=10, pass_threshold=80)
    scores = [100.0] * 9 + [0.0]          # 9/10 pass at threshold 80
    completes = [True] * 10
    stat = summarize_scenario("s.md", scores, completes, policy)
    assert stat.passes == 9
    assert stat.reliability(1) == pytest.approx(0.9)
    assert stat.reliability(10) == 0.0    # can't have 10 all-pass with one failure
