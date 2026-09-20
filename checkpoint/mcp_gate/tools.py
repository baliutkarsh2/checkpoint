"""What Checkpoint can do for a coding agent, as plain functions.

These are the bodies behind the MCP tools in :mod:`checkpoint.mcp_gate.server`,
kept separate so they can be tested without a transport. They mirror the CLI
one-to-one on purpose: an agent that has read the README should be able to guess
these, and an answer it gets here should match what the developer sees when they
run the same thing themselves.

Every one of them can be called with no ``command``, in which case the agent
under test is whatever ``checkpoint.toml`` describes — the same resolution the
CLI does, so a model does not have to invent a command it cannot know.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..project import ConfigError, Project


def _agent(command: str | None):
    """The agent to test: the caller's command, or the project's.

    Raises a plain ``ValueError`` whose message is the answer we want the model
    to read; a stack trace is something it can neither act on nor relay.
    """
    try:
        agent = Project.load().build_agent(command)
    except ConfigError as e:
        raise ValueError(str(e)) from None
    if agent is None:
        raise ValueError(
            "no agent configured: pass command=\"python my_agent.py\", or run "
            "`checkpoint init --command \"...\"` in this project first")
    return agent


def _model(judge_model: str | None) -> str:
    return Project.load().judge_model(judge_model)


def list_scenarios_tool(scenarios_dir: str | None = None) -> list[dict]:
    """Every scenario under a directory, with its task and criteria."""
    from ..scenario import parse_file

    project = Project.load()
    roots = [Path(scenarios_dir)] if scenarios_dir else project.scenario_paths()
    out: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                scenario = parse_file(path)
            except (OSError, ValueError):
                continue
            if not scenario.runnable:
                continue  # ordinary markdown that happens to live here
            out.append({
                "path": str(path),
                "title": scenario.title,
                "task": (scenario.prompt or "")[:400],
                "twins": scenario.twins,
                "criteria": [
                    {"text": c.text, "kind": c.kind, "must_pass": c.must_pass}
                    for c in scenario.criteria
                ],
                "runs": scenario.runs,
            })
    return out


def check_scenario_tool(scenario_path: str) -> dict:
    """How each criterion in a scenario will actually be decided.

    Worth calling before writing or changing criteria: it shows the assertion
    behind each one, which is where a criterion that an agent doing nothing
    would pass gives itself away.
    """
    from ..eval import schema_for
    from ..eval.nl import compile_criterion
    from ..scenario import parse_file
    from ..twins import registry

    scenario = parse_file(scenario_path)
    known = set(registry.names())
    problems = list(scenario.problems)
    if not scenario.prompt:
        problems.append("no '## Task' section")
    if not scenario.criteria:
        problems.append("no '## Criteria' section")
    problems.extend(f"unknown twin {t!r}" for t in scenario.twins if t.lower() not in known)

    schema = (schema_for(scenario.twins, workspace=bool(scenario.workspace))
              if scenario.twins or scenario.workspace else None)
    criteria = []
    for criterion in scenario.criteria:
        assertion, source = criterion.assertion, "pinned" if criterion.assertion else ""
        if assertion is None and criterion.kind != "P" and schema is not None:
            compiled = compile_criterion(criterion.text, schema)
            if compiled is not None:
                assertion, source = compiled.assertion, compiled.source
        criteria.append({
            "text": criterion.text, "kind": criterion.kind,
            "must_pass": criterion.must_pass, "assertion": assertion,
            "decided_by": source or ("judge" if criterion.kind == "P" else "compiled at run time"),
        })
    return {"scenario": str(scenario_path), "title": scenario.title,
            "twins": scenario.twins, "valid": not problems,
            "problems": problems, "criteria": criteria}


def run_scenario_tool(scenario_path: str, command: str | None = None,
                      judge_model: str | None = None) -> dict:
    """Run one scenario once and report what the agent did."""
    from ..engine import RunOptions, run_scenario
    from ..scenario import parse_file

    agent = _agent(command)
    model = _model(judge_model)
    result = run_scenario(parse_file(scenario_path), agent, options=RunOptions(judge_model=model))
    return {
        "score": result.score,
        "scored": result.scored,
        "complete": result.complete,
        "error": result.error,
        "final_answer": result.final_answer,
        "api_calls": len(result.trace),
        "duration_s": round(result.duration_s, 2),
        "criteria": [
            {"text": c.text, "kind": c.kind, "status": c.status, "passed": c.passed,
             "must_pass": c.must_pass, "assertion": c.assertion,
             "decided_by": c.evaluator, "reasoning": c.reasoning}
            for c in result.criteria
        ],
        "problems": result.eval_errors + result.warnings,
    }


def gate_tool(target: str | None = None, command: str | None = None, runs: int = 16,
              pass_threshold: float = 80.0, judge_model: str | None = None) -> dict:
    """Run every scenario ``runs`` times and decide whether the build ships.

    SHIP / CONDITIONAL / INCONCLUSIVE / BLOCK / ERROR — only SHIP means the
    evidence supports a release, and ERROR means the plumbing broke so there is
    no verdict at all. ``evidence`` spells out what each scenario's runs showed,
    including how many runs a SHIP would need. Fewer than 16 runs cannot reach
    SHIP at the default threshold.
    """
    from ..engine import RunOptions
    from ..gate import GatePolicy, run_gate

    project = Project.load()
    root = Path(target) if target else project.scenario_paths()[0]
    try:
        agent = _agent(command)
        policy = GatePolicy(runs=runs, pass_threshold=pass_threshold)
    except ValueError as e:
        # Arguments here were chosen by a model; hand back the problem it can
        # act on rather than an exception it can only relay.
        return _error(f"{e}")
    if not root.exists():
        return _error(f"no scenarios at {root}")

    model = project.judge_model(judge_model)
    result = run_gate(root, None, policy, agent=agent,
                      options=RunOptions(judge_model=model), judge_model=model)
    return {
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "runs_needed_to_ship": policy.min_runs_to_ship,
        "scenarios": [
            {"scenario": s.scenario, "passes": s.passes, "n": s.n,
             "pass_rate": round(s.pass_rate, 4),
             "ci_low": round(s.ci.low, 4), "ci_high": round(s.ci.high, 4),
             "classification": s.classification, "error_runs": s.error_runs,
             "evidence": s.evidence()}
            for s in result.scenarios
        ],
        "skipped": [{"path": s.path, "reason": s.reason} for s in result.skipped],
        "errors": result.errors,
    }


def _error(message: str) -> dict[str, Any]:
    return {"verdict": "ERROR", "exit_code": 4, "scenarios": [], "skipped": [],
            "errors": [message]}
