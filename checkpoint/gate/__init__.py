"""The release gate: run each scenario N times, reason about the pass-rate
distribution, and issue a single SHIP / CONDITIONAL / BLOCK verdict."""
from .verdict import GateResult, ScenarioStat, GatePolicy, decide_verdict
from .engine import run_gate

__all__ = ["GateResult", "ScenarioStat", "GatePolicy", "decide_verdict", "run_gate"]
