"""Statistical primitives for gating non-deterministic agent runs."""
from .intervals import wilson_interval, ProportionCI, classify_stability

__all__ = ["wilson_interval", "ProportionCI", "classify_stability"]
