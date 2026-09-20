"""Score a run: turn each criterion into a verdict, with its reasoning on show.

Every criterion takes one of two routes. A claim about the world becomes an
assertion — written in the scenario, matched by a pattern, or compiled once by a
model — and is then evaluated deterministically. A matter of judgement goes to
the judge. Either way the result records *how* it was decided, so a wrong
verdict can be traced to a wrong translation rather than disappearing into a
score.

An assertion that cannot be evaluated (an unknown field, an ambiguous
selection) is an ERROR, never a pass or a fail: the run has no verdict rather
than a wrong one.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .compile import AssertionCache, compile_with_llm
from .expr import World, evaluate
from .nl import Schema, compile_criterion


@dataclass
class CriterionOutcome:
    text: str
    kind: str
    status: str                 # "pass" | "fail" | "error"
    reasoning: str
    evaluator: str              # "assertion:pinned" | "assertion:pattern" | "assertion:llm" | "judge"
    assertion: str | None = None
    must_pass: bool = False
    uncertain: bool = False
    """The judge answered "unknown": no evidence either way, counted as not passed."""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass
class Evaluation:
    outcomes: list[CriterionOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.outcomes:
            return 0.0
        return 100.0 * sum(1 for o in self.outcomes if o.passed) / len(self.outcomes)

    @property
    def failed_must_pass(self) -> list[CriterionOutcome]:
        return [o for o in self.outcomes if o.must_pass and not o.passed]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors) or any(o.status == "error" for o in self.outcomes)


JudgeFn = Callable[..., Sequence[Any]]


def evaluate_criteria(
    scenario: Any,
    world: World,
    schema: Schema,
    *,
    model: str,
    samples: int = 1,
    cache: AssertionCache | None = None,
    judge: JudgeFn | None = None,
    compile_fn: Callable[..., Any] | None = None,
    allow_llm: bool = True,
) -> Evaluation:
    """Decide every criterion of ``scenario`` against ``world``."""
    evaluation = Evaluation()
    cache = cache if cache is not None else AssertionCache()
    compile_fn = compile_fn or compile_with_llm
    seed_world = _seed_view_of(world)
    deferred: list[tuple[int, Any]] = []

    for index, criterion in enumerate(scenario.criteria):
        assertion, source, note = _assertion_for(
            criterion, schema, seed_world, model=model, cache=cache,
            compile_fn=compile_fn, allow_llm=allow_llm,
        )
        if assertion is None:
            evaluation.outcomes.append(CriterionOutcome(
                text=criterion.text, kind=criterion.kind, status="fail",
                reasoning=note or "waiting for the judge", evaluator="judge",
                must_pass=getattr(criterion, "must_pass", False),
            ))
            deferred.append((index, criterion))
            continue
        outcome = evaluate(assertion, world)
        evaluation.outcomes.append(CriterionOutcome(
            text=criterion.text, kind=criterion.kind, status=outcome.status,
            reasoning=_reasoning(outcome, note), evaluator=f"assertion:{source}",
            assertion=assertion, must_pass=getattr(criterion, "must_pass", False),
        ))

    if deferred:
        _judge_deferred(evaluation, deferred, world, model=model,
                        samples=samples, judge=judge)
    return evaluation


def _assertion_for(
    criterion: Any,
    schema: Schema,
    seed_world: World,
    *,
    model: str,
    cache: AssertionCache,
    compile_fn: Callable[..., Any],
    allow_llm: bool,
) -> tuple[str | None, str, str]:
    """The assertion to evaluate for a criterion, where it came from, and any note."""
    pinned = getattr(criterion, "assertion", None)
    if pinned:
        return pinned, "pinned", ""
    if criterion.kind == "P":
        return None, "", ""  # judgement by definition
    matched = compile_criterion(criterion.text, schema)
    if matched is not None:
        return matched.assertion, matched.source, ""
    if not allow_llm:
        return None, "", "no pattern matched this criterion"
    result = compile_fn(criterion.text, schema, seed_world, model=model, cache=cache)
    if result.compiled is not None:
        return result.compiled.assertion, "llm", result.reason
    return None, "", result.reason


def _judge_deferred(
    evaluation: Evaluation,
    deferred: list[tuple[int, Any]],
    world: World,
    *,
    model: str,
    samples: int,
    judge: JudgeFn | None,
) -> None:
    criteria = [_SimpleCriterion(f"c{index + 1}", c.text) for index, c in deferred]
    if judge is None:
        try:
            from .judge import judge as default_judge
        except ImportError as e:  # no judge configured in this build
            evaluation.errors.append(f"no judge available: {e}")
            for index, _ in deferred:
                outcome = evaluation.outcomes[index]
                outcome.status = "error"
                outcome.reasoning = "no judge available for criteria that need judgement"
            return
        judge = default_judge

    try:
        verdicts = judge(criteria, world, model=model, samples=samples)
    except Exception as e:  # noqa: BLE001 — a judge outage is an error, not a failed agent
        evaluation.errors.append(f"judge failed: {e}")
        for index, _ in deferred:
            outcome = evaluation.outcomes[index]
            outcome.status = "error"
            outcome.reasoning = f"judge failed: {e}"
        return

    by_id = {getattr(v, "id", ""): v for v in verdicts}
    for index, _ in deferred:
        outcome = evaluation.outcomes[index]
        verdict = by_id.get(f"c{index + 1}")
        if verdict is None:
            outcome.status = "error"
            outcome.reasoning = "the judge returned no verdict for this criterion"
            continue
        if getattr(verdict, "error", None):
            outcome.status = "error"
            outcome.reasoning = str(verdict.error)
            continue
        passed = getattr(verdict, "passed", None)
        reasoning = getattr(verdict, "reasoning", "") or ""
        evidence = getattr(verdict, "evidence", None)
        if evidence:
            reasoning = f"{reasoning} [{evidence}]".strip()
        if passed is None:
            outcome.status, outcome.uncertain = "fail", True
            outcome.reasoning = f"the judge could not decide: {reasoning}".strip()
        else:
            outcome.status = "pass" if passed else "fail"
            outcome.reasoning = reasoning


@dataclass
class _SimpleCriterion:
    id: str
    text: str


def _reasoning(outcome: Any, note: str) -> str:
    detail = outcome.detail or ""
    if outcome.status == "error":
        return detail
    if note and outcome.status == "fail":
        return f"{detail} ({note})" if detail else note
    return detail


def _seed_view_of(world: World) -> World:
    """The same world with the seed as its final state, for compile-time checks."""
    return World(
        final=world.seed, seed=world.seed, keys=world.keys, tombstones=world.tombstones,
        trace=[], egress=[], answer="", exit_code=world.exit_code, duration=0.0,
    )
