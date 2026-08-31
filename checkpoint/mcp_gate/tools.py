"""Plain implementations behind the Checkpoint MCP tools (unit-testable)."""
from __future__ import annotations

import os
import shlex
from pathlib import Path


def _split(harness: str) -> list[str]:
    return shlex.split(harness, posix=(os.name != "nt"))


def list_scenarios_tool(scenarios_dir: str = "scenarios") -> list[dict]:
    """List scenarios under a directory with their prompt and criteria count."""
    from ..scenario import parse_file

    base = Path(scenarios_dir)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*.md")):
        try:
            scn = parse_file(p)
        except Exception:
            continue
        out.append({
            "path": str(p),
            "prompt": (scn.prompt or "")[:200],
            "criteria": len(scn.criteria),
            "clones": scn.clones,
        })
    return out


def run_scenario_tool(scenario_path: str, harness: str,
                      judge_model: str = "gpt-4o-mini") -> dict:
    """Run one scenario against the harness command; return score + criteria."""
    from ..runner import run_once
    from ..scenario import parse_file

    scn = parse_file(scenario_path)
    result = run_once(scn, _split(harness), judge_model=judge_model)
    return {
        "score": result.score,
        "complete": result.complete,
        "error": result.error,
        "final_answer": result.final_answer,
        "criteria": [
            {"text": c.text, "kind": c.kind, "passed": c.passed,
             "evaluator": c.evaluator, "reasoning": c.reasoning}
            for c in result.criteria
        ],
    }


def gate_tool(target: str, harness: str, runs: int = 10,
              pass_threshold: float = 80.0, judge_model: str = "gpt-4o-mini") -> dict:
    """Gate a scenario or directory N times; return the SHIP/CONDITIONAL/BLOCK verdict."""
    from ..gate import GatePolicy, run_gate

    policy = GatePolicy(runs=runs, pass_threshold=pass_threshold)
    result = run_gate(Path(target), _split(harness), policy, judge_model=judge_model)
    return {
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "scenarios": [
            {"scenario": s.scenario, "pass_rate": round(s.pass_rate, 4),
             "ci_low": round(s.ci.low, 4), "ci_high": round(s.ci.high, 4),
             "classification": s.classification}
            for s in result.scenarios
        ],
        "errors": result.errors,
    }
