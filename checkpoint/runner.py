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

from .engine.agent import extract_answer
from .llm import DEFAULT_MODEL
from .scenario import Scenario
from .twins import registry as twin_registry

if TYPE_CHECKING:
    from .engine import Agent, RunOptions


@dataclass
class CriterionResult:
    text: str
    kind: str
    passed: bool
    reasoning: str
    evaluator: str  # "assertion:pattern" | "assertion:llm" | "assertion:pinned" | "judge" | ...
    status: str = ""
    """"pass", "fail", or "error" — an error means the criterion could not be decided."""
    assertion: str | None = None
    """The assertion that decided it, when it was decided deterministically."""
    must_pass: bool = False
    uncertain: bool = False

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "pass" if self.passed else "fail"


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
    eval_errors: list[str] = field(default_factory=list)
    """Scoring problems (a judge outage, an assertion that cannot be evaluated)."""

    @property
    def score(self) -> float:
        if not self.criteria:
            return 0.0
        return 100.0 * sum(1 for c in self.criteria if c.passed) / len(self.criteria)

    @property
    def complete(self) -> bool:
        return self.error is None and self.exit_code == 0

    @property
    def scored(self) -> bool:
        """Whether this run produced a usable verdict (no scoring errors)."""
        return (self.complete and not self.eval_errors
                and all(c.status != "error" for c in self.criteria))

    @property
    def failed_must_pass(self) -> list[CriterionResult]:
        return [c for c in self.criteria if c.must_pass and not c.passed]


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
    """Score ``result`` against ``scenario``'s criteria (see :mod:`checkpoint.eval`)."""
    from .eval import Schema, build_world
    from .eval.evaluate import evaluate_criteria

    world = build_world(
        seed_views=result.seed_views,
        final_views=result.views,
        trace=result.trace,
        answer=result.final_answer,
        egress=result.egress,
        exit_code=result.exit_code,
        duration=result.duration_s,
    )
    schema = Schema.from_views(result.views or result.seed_views)
    evaluation = evaluate_criteria(scenario, world, schema, model=judge_model)
    result.criteria = [
        CriterionResult(
            text=o.text, kind=o.kind, passed=o.passed, reasoning=o.reasoning,
            evaluator=o.evaluator, status=o.status, assertion=o.assertion,
            must_pass=o.must_pass, uncertain=o.uncertain,
        )
        for o in evaluation.outcomes
    ]
    result.eval_errors.extend(evaluation.errors)
