"""The release gate: run each scenario N times, reason about the pass-rate
distribution, and issue a single verdict — SHIP, CONDITIONAL, INCONCLUSIVE,
BLOCK, or ERROR. Only SHIP exits 0."""
from .engine import (
    collect_scenarios,
    default_concurrency,
    judge_credential_error,
    run_gate,
)
from .verdict import (
    EXIT_CODES,
    GatePolicy,
    GateResult,
    ScenarioStat,
    SkippedScenario,
    decide_verdict,
    exit_code_for,
)

__all__ = [
    "EXIT_CODES",
    "GatePolicy",
    "GateResult",
    "ScenarioStat",
    "SkippedScenario",
    "collect_scenarios",
    "default_concurrency",
    "decide_verdict",
    "exit_code_for",
    "judge_credential_error",
    "run_gate",
]
