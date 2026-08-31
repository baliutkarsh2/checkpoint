"""Summary metrics over a trajectory — the path-quality signals that a
final-state check misses."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .model import Trajectory


@dataclass
class TrajectoryMetrics:
    total_calls: int = 0
    read_calls: int = 0
    write_calls: int = 0
    error_calls: int = 0
    distinct_endpoints: int = 0
    redundant_calls: int = 0            # identical method+path issued more than once
    max_repeat: int = 0                 # how many times the most-repeated call fired
    methods: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "read_calls": self.read_calls,
            "write_calls": self.write_calls,
            "error_calls": self.error_calls,
            "distinct_endpoints": self.distinct_endpoints,
            "redundant_calls": self.redundant_calls,
            "max_repeat": self.max_repeat,
            "methods": self.methods,
        }


def compute_metrics(trajectory: Trajectory) -> TrajectoryMetrics:
    m = TrajectoryMetrics()
    m.total_calls = len(trajectory.steps)
    sig_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    for step in trajectory.steps:
        if step.is_write:
            m.write_calls += 1
        elif step.method == "GET":
            m.read_calls += 1
        if step.is_error:
            m.error_calls += 1
        sig_counts[step.signature] += 1
        if step.method:
            method_counts[step.method] += 1
    m.distinct_endpoints = len(sig_counts)
    # Redundant = every call beyond the first for each identical signature.
    m.redundant_calls = sum(c - 1 for c in sig_counts.values() if c > 1)
    m.max_repeat = max(sig_counts.values(), default=0)
    m.methods = dict(method_counts)
    return m
