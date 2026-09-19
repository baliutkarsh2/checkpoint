"""Run results, scoring, and the ``run_once`` entry point.

``run_once`` executes one scenario against one agent command. It is a thin
wrapper over :func:`checkpoint.engine.run_scenario`, which owns the sandbox and
the agent process; this module keeps the result types and criterion scoring.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from checkpoint.fake_credentials import FAKE_TOKENS

from .checker import check
from .checker_llm import try_stage2
from .engine.agent import extract_answer
from .eval import JudgeCriterion, Verdict, build_world, judge
from .llm import DEFAULT_MODEL
from .scenario import Criterion, Scenario
from .twins import registry as twin_registry

if TYPE_CHECKING:
    from .engine import Agent, RunOptions


@dataclass
class CriterionResult:
    text: str
    kind: str
    passed: bool
    reasoning: str
    evaluator: str  # "deterministic", "llm-json", "trajectory", "llm", ...


@dataclass
class RunResult:
    final_answer: str
    stderr: str
    exit_code: int
    trace: list
    state: dict
    stdout: str = ""
    criteria: list[CriterionResult] = field(default_factory=list)
    error: str | None = None
    run_id: str = ""
    agent: str = ""
    twins: list[str] = field(default_factory=list)
    seed_views: dict = field(default_factory=dict)
    """Each twin's collections before the agent ran (``{twin: {collection: view}}``)."""
    views: dict = field(default_factory=dict)
    """Each twin's collections after the agent ran."""
    egress: list[dict] = field(default_factory=list)
    """Connections to hosts outside the sandbox (allowed or blocked)."""
    duration_s: float = 0.0
    timed_out: bool = False
    setup_error: bool = False
    """True when the sandbox (not the agent) failed; such runs carry no verdict."""
    warnings: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.criteria:
            return 0.0
        return 100.0 * sum(1 for c in self.criteria if c.passed) / len(self.criteria)

    @property
    def complete(self) -> bool:
        return self.error is None and self.exit_code == 0


def twin_mcp_url(port: int | str, host: str = "127.0.0.1") -> str:
    """The MCP endpoint every twin mounts next to its REST API."""
    return f"http://{host}:{port}/mcp/"


# Kept for the Docker runner, which exports each twin's credential under this name.
_CLONE_BOOTSTRAP_TOKEN_ENV = {
    spec.name: (spec.token_env[0], FAKE_TOKENS[spec.name])
    for spec in twin_registry.all_specs() if spec.token_env
}

_extract_final_answer = extract_answer


def run_once(
    scenario: Scenario,
    harness_cmd: Sequence[str] | str,
    cwd: str | None = None,
    judge_model: str = DEFAULT_MODEL,
    *,
    agent: Agent | None = None,
    options: RunOptions | None = None,
) -> RunResult:
    """Run ``scenario`` once against the agent started by ``harness_cmd``."""
    from .engine import Agent, RunOptions, run_scenario

    agent = agent or Agent(command=harness_cmd if isinstance(harness_cmd, str) else list(harness_cmd),
                           cwd=cwd)
    opts = options or RunOptions(judge_model=judge_model)
    return run_scenario(scenario, agent, options=opts)


def _merge_state_for_clones(per_clone_state: dict[str, dict]) -> dict:
    """Build the `state` field on RunResult for multi-clone runs.

    Single-clone runs keep the legacy flat shape (top-level keys are twin state
    keys like `repositories`, `pull_requests`, etc.) so deterministic checks
    against existing scenarios keep working. Multi-clone runs use a nested
    `{clone_id: state}` shape and the deterministic checker walks both.
    """
    if len(per_clone_state) == 1:
        return next(iter(per_clone_state.values()))
    return dict(per_clone_state)


def _merge_trace_for_clones(per_clone_trace: dict[str, list]) -> list:
    """Concatenate per-clone traces. Each entry is tagged with `_clone` so
    callers can filter when needed."""
    if len(per_clone_trace) == 1:
        return next(iter(per_clone_trace.values()))
    out: list = []
    for clone, entries in per_clone_trace.items():
        for e in entries:
            if isinstance(e, dict) and "_clone" not in e:
                e = {**e, "_clone": clone}
            out.append(e)
    return out


def _parse_seed_spec(raw: str | None, clones: list[str]) -> dict[str, str]:
    """Parse `seed:` / `seed-file:` config into a {clone: value} map.

    Single value (no `=`) applies to the first clone only (legacy v0 behavior).
    Comma-separated `clone=value` pairs apply per-clone. Unknown clones are
    ignored silently — they may be intentionally excluded from a run.
    """
    if not raw:
        return {}
    raw = raw.strip()
    if "=" not in raw:
        # Single value: apply to first clone (legacy behavior).
        return {clones[0]: raw} if clones else {}
    out: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k and v:
            out[k] = v
    return out


def _evaluate(scenario: Scenario, result: RunResult, judge_model: str) -> None:
    deferred: list[Criterion] = []
    for c in scenario.criteria:
        if c.kind == "D":
            # Stage 1: regex catalog.
            cr = check(c.text, result.state, result.trace)
            if cr.handled:
                result.criteria.append(CriterionResult(
                    text=c.text, kind="D", passed=cr.passed,
                    reasoning=cr.reasoning, evaluator="deterministic",
                ))
                continue

            # Stage 2: schema-validated LLM-JSON parser (the wedge).
            # On any fall-through (invalid JSON, schema fail, unknown noun,
            # missing API key) we defer to the `[P]` judge with the ORIGINAL
            # text — no silent failure. Reason is recorded for debugging.
            try:
                stage2_result, stage2_reason = try_stage2(
                    c.text, result.state, result.trace, model=judge_model,
                )
            except Exception as e:
                stage2_result, stage2_reason = None, f"stage2 raised: {e}"
            if stage2_result is not None:
                result.criteria.append(CriterionResult(
                    text=c.text, kind="D", passed=stage2_result.passed,
                    reasoning=stage2_result.reasoning, evaluator="llm-json",
                ))
                continue
            # Tag the criterion with the fall-through reason so the run record
            # tells us *why* stage 2 didn't carry it.
            c = Criterion(text=c.text, kind=c.kind)
            c._stage2_fallthrough = stage2_reason
            deferred.append(c)
        elif c.kind == "T":
            # Trajectory criteria: evaluate the agent's call path deterministically.
            from .trajectory import Trajectory, compute_metrics
            from .trajectory.checker import check as _check_traj

            traj = Trajectory.from_trace(result.trace)
            passed, reasoning = _check_traj(c.text, traj, compute_metrics(traj))
            if passed is not None:
                result.criteria.append(CriterionResult(
                    text=c.text, kind="T", passed=passed,
                    reasoning=reasoning, evaluator="trajectory",
                ))
                continue
            deferred.append(c)  # unrecognized phrasing -> judge
        else:
            deferred.append(c)

    if not deferred:
        return

    # The id is positional but opaque: it exists so a verdict can only come back
    # attached to the criterion that produced it, never matched by its wording.
    criteria = [JudgeCriterion(id=f"c{i}", text=c.text) for i, c in enumerate(deferred)]
    world = build_world(
        seed_views=result.seed_views,
        final_views=result.views,
        trace=result.trace,
        answer=result.final_answer,
        task=scenario.prompt,
        egress=result.egress,
        exit_code=result.exit_code,
        duration=result.duration_s,
    )
    verdicts = judge(criteria, world, model=judge_model)

    for c, v in zip(deferred, verdicts, strict=True):
        result.criteria.append(CriterionResult(
            # An undecided criterion is not a pass: a gate that cannot tell must
            # not ship, and the reasoning carries why so it is visible, not silent.
            text=c.text, kind=c.kind, passed=v.passed is True,
            reasoning=_reason(v), evaluator="llm",
        ))


def _reason(verdict: Verdict) -> str:
    """The verdict as one line, keeping the cited evidence with the claim."""
    if verdict.error:
        return f"Judge error: {verdict.error}"
    if verdict.passed is None:
        return f"Judge could not decide: {verdict.reasoning}"
    if verdict.evidence:
        return f"{verdict.reasoning} (evidence: {verdict.evidence})"
    return verdict.reasoning
