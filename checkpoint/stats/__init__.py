"""Statistical primitives for gating non-deterministic agent runs."""
from .intervals import (
    TOLERANCE,
    ProportionCI,
    classify_stability,
    is_regression,
    runs_needed,
    wilson_interval,
    z_for,
)
from .reliability import pass_hat_k

__all__ = [
    "TOLERANCE",
    "ProportionCI",
    "classify_stability",
    "is_regression",
    "pass_hat_k",
    "runs_needed",
    "wilson_interval",
    "z_for",
]
