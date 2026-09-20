"""What a run produced, and how a criterion becomes a verdict on it.

:func:`checkpoint.engine.run_scenario` owns the sandbox and the agent process
and returns a :class:`RunResult`; this module owns that result type and the
scoring applied to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .scenario import Scenario

if TYPE_CHECKING:
    pass


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
    agent_trace: list = field(default_factory=list)
    """Events the agent wrote to ``$CHECKPOINT_AGENT_TRACE_FILE``, if any."""
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


def merge_state_for_twins(per_twin_state: dict[str, dict]) -> dict:
    """Build the `state` field on RunResult for a run with several twins.

    A single twin keeps the flat shape — top-level keys are that twin's own
    state keys, `repositories`, `pull_requests` and so on — so criteria written
    against one twin read the same whether or not others are present. Several
    twins nest under `{twin: state}`, and the deterministic checker walks both.
    """
    if len(per_twin_state) == 1:
        return next(iter(per_twin_state.values()))
    return dict(per_twin_state)


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
        task=scenario.prompt,
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
