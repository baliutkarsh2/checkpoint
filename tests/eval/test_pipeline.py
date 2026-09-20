"""How a criterion reaches a verdict: pinned, pattern, compiled, or judged."""
from __future__ import annotations

from checkpoint.eval import Schema, build_world
from checkpoint.eval.compile import AssertionCache, CompileResult, compile_with_llm
from checkpoint.eval.evaluate import evaluate_criteria
from checkpoint.eval.nl import Compiled
from checkpoint.scenario import parse

SEED_VIEWS = {
    "github": {
        "issues": {"key": "id", "tombstone": None, "nouns": ["issue", "issues"],
                   "fields": ["id", "number", "title", "state", "labels"],
                   "items": [{"id": 1, "number": 1, "title": "Old", "state": "open", "labels": []}]},
    },
}
FINAL_VIEWS = {
    "github": {
        "issues": {"key": "id", "tombstone": None, "nouns": ["issue", "issues"],
                   "fields": ["id", "number", "title", "state", "labels"],
                   "items": [
                       {"id": 1, "number": 1, "title": "Old", "state": "open", "labels": []},
                       {"id": 2, "number": 2, "title": "Launch coordination", "state": "open",
                        "labels": [{"name": "launch"}]},
                   ]},
    },
}


def _world(answer: str = "Filed issue #2"):
    return build_world(seed_views=SEED_VIEWS, final_views=FINAL_VIEWS, trace=[], answer=answer)


def _scenario(*criteria: str) -> object:
    body = "# s\n## Task\ndo it\n## Criteria\n" + "".join(f"- {c}\n" for c in criteria)
    return parse(body + "## Config\ntwins: github\n")


def _judge(verdicts: dict[str, bool]):
    def judge(criteria, world, *, model, **kwargs):
        return [
            type("V", (), {"id": c.id, "passed": verdicts.get(c.text), "reasoning": "because",
                           "evidence": "answer", "error": None})()
            for c in criteria
        ]
    return judge


def test_each_route_is_taken_and_reported():
    scenario = _scenario(
        '[D] An issue titled "Launch coordination" exists',          # pattern
        '[D] Exactly 1 issue was created',                            # pattern, delta
        '[D] The launch issue carries the launch label => exists(github.issues[number == 2])',  # pinned
        "[P] The final answer is clear and friendly",                 # judged
    )
    evaluation = evaluate_criteria(
        scenario, _world(), Schema.from_views(FINAL_VIEWS), model="test",
        judge=_judge({"The final answer is clear and friendly": True}), allow_llm=False,
    )
    sources = [o.evaluator for o in evaluation.outcomes]
    assert sources == ["assertion:pattern", "assertion:pattern", "assertion:pinned", "judge"]
    assert all(o.passed for o in evaluation.outcomes), [(o.text, o.reasoning) for o in evaluation.outcomes]
    assert evaluation.score == 100.0
    assert evaluation.outcomes[1].assertion == "count(created.github.issues) == 1"


def test_unmatched_criterion_is_compiled_then_evaluated():
    scenario = _scenario("[D] At least one issue carries the launch label")
    calls: list[str] = []

    def fake_compile(text, schema, seed_world, *, model, cache):
        calls.append(text)
        return CompileResult(Compiled('exists(github.issues[labels[*].name contains "launch"])', "llm"))

    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", compile_fn=fake_compile, judge=_judge({}))
    assert calls == ["At least one issue carries the launch label"]
    outcome = evaluation.outcomes[0]
    assert outcome.evaluator == "assertion:llm" and outcome.passed
    assert outcome.assertion.startswith("exists(")


def test_criterion_needing_judgement_is_not_compiled():
    scenario = _scenario("[D] The agent was polite about the outage")

    def fake_compile(text, schema, seed_world, *, model, cache):
        return CompileResult(None, "this is a matter of judgement")

    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", compile_fn=fake_compile,
                                   judge=_judge({"The agent was polite about the outage": True}))
    assert evaluation.outcomes[0].evaluator == "judge" and evaluation.outcomes[0].passed


def test_a_judge_outage_is_an_error_not_a_failed_agent():
    scenario = _scenario("[P] The answer explains what happened")

    def broken_judge(criteria, world, *, model, **kwargs):
        raise RuntimeError("no API key configured")

    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", judge=broken_judge, allow_llm=False)
    assert evaluation.outcomes[0].status == "error"
    assert evaluation.has_errors and "no API key" in evaluation.outcomes[0].reasoning


def test_missing_verdict_is_an_error_not_a_pass():
    scenario = _scenario("[P] a", "[P] b")

    def forgetful(criteria, world, *, model, **kwargs):
        first = criteria[0]
        return [type("V", (), {"id": first.id, "passed": True, "reasoning": "ok",
                               "evidence": None, "error": None})()]

    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", judge=forgetful, allow_llm=False)
    assert [o.status for o in evaluation.outcomes] == ["pass", "error"]


def test_unknown_verdict_counts_as_not_passed_and_is_flagged():
    scenario = _scenario("[P] The answer is accurate")

    def unsure(criteria, world, *, model, **kwargs):
        return [type("V", (), {"id": c.id, "passed": None, "reasoning": "no evidence either way",
                               "evidence": None, "error": None})() for c in criteria]

    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", judge=unsure, allow_llm=False)
    outcome = evaluation.outcomes[0]
    assert outcome.status == "fail" and outcome.uncertain
    assert "could not decide" in outcome.reasoning


def test_must_pass_is_tracked_separately_from_the_score():
    scenario = _scenario(
        "[D!] No issues were deleted => count(deleted.github.issues) == 1",
        '[D] An issue titled "Launch coordination" exists',
    )
    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", judge=_judge({}), allow_llm=False)
    assert evaluation.score == 50.0
    assert [o.text for o in evaluation.failed_must_pass] == ["No issues were deleted"]


def test_broken_assertion_is_an_error():
    scenario = _scenario("[D] whatever => count(github.gizmos) == 0")
    evaluation = evaluate_criteria(scenario, _world(), Schema.from_views(FINAL_VIEWS),
                                   model="test", judge=_judge({}), allow_llm=False)
    assert evaluation.outcomes[0].status == "error"
    assert "no collection 'gizmos'" in evaluation.outcomes[0].reasoning


# -- the LLM compiler itself ----------------------------------------------------

def test_compiler_rejects_an_assertion_that_names_something_unknown(tmp_path):
    schema = Schema.from_views(FINAL_VIEWS)
    seed = build_world(seed_views=SEED_VIEWS, final_views=SEED_VIEWS, trace=[])
    answers = [
        {"assertion": 'exists(github.tickets[title == "x"])', "reason": "guessing"},
        {"assertion": 'exists(github.issues[title == "x"])', "reason": "second try"},
    ]

    def fake_complete(*, model, system, user, schema=None):
        return answers.pop(0)

    result = compile_with_llm("an issue titled x exists", schema, seed, model="test",
                              cache=AssertionCache(tmp_path / "c.json"), complete=fake_complete)
    assert result.compiled is not None
    assert result.compiled.assertion == 'exists(github.issues[title == "x"])'


def test_compiler_caches_so_every_run_of_a_gate_scores_the_same(tmp_path):
    schema = Schema.from_views(FINAL_VIEWS)
    seed = build_world(seed_views=SEED_VIEWS, final_views=SEED_VIEWS, trace=[])
    cache = AssertionCache(tmp_path / "c.json")
    calls = 0

    def fake_complete(*, model, system, user, schema=None):
        nonlocal calls
        calls += 1
        return {"assertion": "count(github.issues) == 2", "reason": "counts issues"}

    for _ in range(3):
        result = compile_with_llm("two issues exist in total", schema, seed, model="test",
                                  cache=cache, complete=fake_complete)
        assert result.compiled.assertion == "count(github.issues) == 2"
    assert calls == 1


def test_compiler_reports_a_criterion_that_needs_judgement(tmp_path):
    schema = Schema.from_views(FINAL_VIEWS)
    seed = build_world(seed_views=SEED_VIEWS, final_views=SEED_VIEWS, trace=[])

    def fake_complete(*, model, system, user, schema=None):
        return {"assertion": None, "reason": "tone is not a fact about state"}

    result = compile_with_llm("the agent was polite", schema, seed, model="test",
                              cache=AssertionCache(tmp_path / "c.json"), complete=fake_complete)
    assert result.compiled is None and "tone" in result.reason


def test_compiler_failure_does_not_break_the_run(tmp_path):
    schema = Schema.from_views(FINAL_VIEWS)
    seed = build_world(seed_views=SEED_VIEWS, final_views=SEED_VIEWS, trace=[])

    def boom(*, model, system, user, schema=None):
        raise RuntimeError("no API key configured")

    result = compile_with_llm("something", schema, seed, model="test",
                              cache=AssertionCache(tmp_path / "c.json"), complete=boom)
    assert result.compiled is None and "unavailable" in result.reason
