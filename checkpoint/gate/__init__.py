"""The release gate: run each scenario N times, reason about the pass-rate
distribution, and issue a single SHIP / CONDITIONAL / BLOCK verdict."""
from .engine import run_gate
from .verdict import GatePolicy, GateResult, ScenarioStat, decide_verdict

__all__ = ["GateResult", "ScenarioStat", "GatePolicy", "decide_verdict", "run_gate"]
