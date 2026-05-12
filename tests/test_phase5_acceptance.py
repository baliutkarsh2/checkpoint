"""Phase 5 / Plan 05-04: end-to-end acceptance test for the eval pipeline.

Drives the runner's evaluator directly (no twin processes, no live LLM):
  - Stage 1 (regex)  -> deterministic verdict
  - Stage 2 (LLM-JSON) -> mocked happy path -> llm-json verdict
  - Stage 2 fall-through -> [P] judge with original text -> llm verdict
  - Failure analysis -> per-criterion "why" paragraph
  - Run record -> persisted at tmp cache root with last-run pointer
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from checkpoint.runner import RunResult, _evaluate
from checkpoint.scenario import Criterion, Scenario


@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Resp:
    choices: list


class _FakeCompletions:
    """Sequential responder. Each call pops the next canned response."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        if not self.responses:
            raise RuntimeError("no canned response left")
        return _Resp(choices=[_Choice(message=_Msg(content=self.responses.pop(0)))])


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class FakeOpenAI:
    def __init__(self, responses):
        self.chat = _FakeChat(responses)


def make_scenario(criteria: list[Criterion]) -> Scenario:
    s = Scenario()
    s.title = "phase 5 acceptance"
    s.prompt = "Do a coordinated cross-team launch."
    s.criteria = criteria
    return s


def make_state() -> dict:
    """Synthetic merged state mimicking what the runner would fetch after the
    multi-clone harness ran the seeded launch coordination scenario."""
    return {
        "github": {
            "issues": {
                "1": {"id": 1, "title": "Launch coordination",
                       "state": "open", "comments": 0},
                "2": {"id": 2, "title": "Old bug",
                       "state": "closed", "comments": 1},
            },
            "labels": {}, "repos": {}, "pulls": {},
            "workflow_runs": {}, "comments": {},
        },
        "slack": {
            "channels": {"C1": {"id": "C1", "name": "engineering"}},
            "messages": {"C1": [{"text": "Launch wired up"}]},
            "users": {},
        },
        "stripe": {
            "refunds": {"re_1": {"id": "re_1", "status": "succeeded"}},
            "customers": {}, "products": {}, "prices": {},
            "payment_intents": {}, "invoices": {}, "subscriptions": {},
            "coupons": {}, "payment_links": {}, "disputes": {},
        },
    }


# ---------------------------------------------------------------------------
# End-to-end: stage 1 + stage 2 + [P]
# ---------------------------------------------------------------------------

def test_three_stage_pipeline_end_to_end(monkeypatch):
    # Criteria: 2 hit stage 1, 1 hits stage 2 (mock OK), 1 falls through to [P],
    # and 1 is straight [P].
    scn = make_scenario([
        Criterion(text="An issue titled \"Launch coordination\" exists", kind="D"),
        Criterion(text="Exactly 1 refund exists", kind="D"),
        Criterion(text="The launch issue carries the priority/launch label", kind="D"),  # stage 2 catch
        Criterion(text="The agent's final answer is concise and friendly", kind="D"),    # stage 2 fall-through -> [P]
        Criterion(text="The agent's final answer references github, slack, and stripe", kind="P"),
    ])

    # Stage-2 fake: criterion #3 returns valid JSON (no items match -> fail);
    # criterion #4 returns chatty prose -> fall-through to [P].
    stage2_json_valid = json.dumps({
        "resource": "issues",
        "selector": {"label": "launch"},
        "operator": "exists",
        "value": None,
    })
    stage2_chatty = "Sure! This one's about tone, not state."

    # [P] judge fake: gets criteria #4 and #5 (both deferred to it).
    judge_response = json.dumps({"results": [
        {"criterion": "The agent's final answer is concise and friendly",
         "passed": True, "reasoning": "Tone is polite."},
        {"criterion": "The agent's final answer references github, slack, and stripe",
         "passed": True, "reasoning": "All three referenced."},
    ]})

    stage2_client = FakeOpenAI([stage2_json_valid, stage2_chatty])
    judge_client = FakeOpenAI([judge_response])

    # Patch the OpenAI imports inside each module to use our fakes.
    import checkpoint.checker_llm as checker_llm
    import checkpoint.judge as judge_mod

    monkeypatch.setattr(checker_llm, "OpenAI", lambda: stage2_client, raising=False)
    # checker_llm imports OpenAI inside parse_assertion, so we monkey-patch
    # the lazy import path: replace the module-level "openai" attr.
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: stage2_client)

    # judge.py imports OpenAI inside `judge()`. After we patched openai.OpenAI
    # above, both call sites resolve through the same shim. But each call has
    # a fresh client, so we need a single fake that returns both stage2 and
    # judge responses. Build a unified fake:
    unified = FakeOpenAI([stage2_json_valid, stage2_chatty, judge_response])
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: unified)

    state = make_state()
    result = RunResult(
        final_answer="Coordinated launch across github, slack, stripe.",
        stderr="", exit_code=0, trace=[], state=state,
    )
    _evaluate(scn, result, judge_model="gpt-4o-mini")

    by_text = {c.text: c for c in result.criteria}

    # Stage 1 hits.
    c1 = by_text['An issue titled "Launch coordination" exists']
    assert c1.evaluator == "deterministic"
    assert c1.passed

    c2 = by_text["Exactly 1 refund exists"]
    assert c2.evaluator == "deterministic"
    assert c2.passed

    # Stage 2 happy path: valid JSON -> programmatic verdict. No item has
    # the "launch" label so the verdict is fail (but handled).
    c3 = by_text["The launch issue carries the priority/launch label"]
    assert c3.evaluator == "llm-json"
    assert c3.passed is False

    # Stage 2 fall-through: chatty -> [P] judge with original text.
    c4 = by_text["The agent's final answer is concise and friendly"]
    assert c4.evaluator == "llm"
    assert c4.passed

    # Straight [P]: original kind preserved.
    c5 = by_text["The agent's final answer references github, slack, and stripe"]
    assert c5.evaluator == "llm"
    assert c5.passed


def test_stage2_unknown_noun_falls_through_to_judge(monkeypatch):
    """Schema-valid JSON but unknown resource -> [P] judge takes over."""
    scn = make_scenario([
        Criterion(text="No new gizmos were created", kind="D"),
    ])
    bad = json.dumps({
        "resource": "gizmos",  # unknown noun
        "selector": None,
        "operator": "count_eq",
        "value": 0,
    })
    judge_response = json.dumps({"results": [
        {"criterion": "No new gizmos were created",
         "passed": True, "reasoning": "Out-of-scope -> trivially true."},
    ]})
    unified = FakeOpenAI([bad, judge_response])
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: unified)

    result = RunResult("ans", "", 0, [], make_state())
    _evaluate(scn, result, judge_model="gpt-4o-mini")
    c = result.criteria[0]
    assert c.evaluator == "llm"
    assert c.passed


# ---------------------------------------------------------------------------
# Run-record persistence + failure analysis
# ---------------------------------------------------------------------------

def test_persist_record_with_failure_analysis(tmp_path: Path, monkeypatch):
    """Drive cli._persist_run_record with a real RunResult + fake LLM client.

    Verifies failure analysis is invoked for failed criteria, the run record
    is written to the cache, and the last-run pointer is updated.
    """
    # Build a result with one failed criterion to trigger the analyzer.
    from checkpoint.runner import CriterionResult

    result = RunResult(
        final_answer="done", stderr="", exit_code=0, trace=[{"i": 0}], state={"x": 1},
    )
    result.criteria = [
        CriterionResult("c1", "D", True, "ok", "deterministic"),
        CriterionResult("c2", "D", False, "missing", "llm-json"),
    ]

    failure_json = json.dumps({"analyses": [
        {"criterion": "c2", "why": "Trace entry 0 did the wrong thing."},
    ]})
    unified = FakeOpenAI([failure_json])
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: unified)

    # Redirect CACHE_ROOT to tmp_path so the test doesn't touch repo state.
    import checkpoint.run_record as run_record_mod
    monkeypatch.setattr(run_record_mod, "CACHE_ROOT", tmp_path / ".checkpoint/cache")
    monkeypatch.setattr(run_record_mod, "RUNS_DIR", tmp_path / ".checkpoint/cache/runs")
    monkeypatch.setattr(run_record_mod, "LAST_RUN_POINTER", tmp_path / ".checkpoint/cache/last-run.json")

    from checkpoint.cli import _persist_run_record

    _persist_run_record(
        result,
        scenario_name="phase5-acceptance",
        scenario_path=str(tmp_path / "scn.md"),
        evaluator_model="gpt-4o-mini",
        evaluator_model_source="default",
        task="acceptance scenario",
    )

    cache_root = (tmp_path / ".checkpoint/cache").resolve()
    pointer = cache_root / "last-run.json"
    assert pointer.exists()
    ptr = json.loads(pointer.read_text())
    rid = ptr["run_id"]
    record_path = cache_root / "runs" / f"{rid}.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    assert record["satisfaction"] == 50.0  # 1/2 passing
    assert record["failure_analysis"]["c2"].startswith("Trace entry 0")
    assert record["evaluator_model"] == "gpt-4o-mini"
    assert record["evaluator_model_source"] == "default"
    assert {c["evaluator"] for c in record["criteria"]} == {"deterministic", "llm-json"}


def test_persist_record_no_failures_no_analysis(tmp_path: Path, monkeypatch):
    """Perfect score -> failure analysis dict is None on the record."""
    from checkpoint.runner import CriterionResult

    result = RunResult("done", "", 0, [], {})
    result.criteria = [CriterionResult("c1", "D", True, "ok", "deterministic")]

    # If failure analysis is called we'd see this; the test asserts it isn't.
    import openai as openai_mod
    monkeypatch.setattr(
        openai_mod, "OpenAI", lambda: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )

    import checkpoint.run_record as run_record_mod
    monkeypatch.setattr(run_record_mod, "CACHE_ROOT", tmp_path / ".checkpoint/cache")
    monkeypatch.setattr(run_record_mod, "RUNS_DIR", tmp_path / ".checkpoint/cache/runs")
    monkeypatch.setattr(run_record_mod, "LAST_RUN_POINTER", tmp_path / ".checkpoint/cache/last-run.json")

    from checkpoint.cli import _persist_run_record

    _persist_run_record(
        result,
        scenario_name="ok",
        scenario_path=None,
        evaluator_model="m",
        evaluator_model_source="default",
        task="t",
    )

    pointer = (tmp_path / ".checkpoint/cache/last-run.json").resolve()
    record_path = (tmp_path / ".checkpoint/cache/runs" /
                   f"{json.loads(pointer.read_text())['run_id']}.json")
    record = json.loads(record_path.read_text())
    assert record["satisfaction"] == 100.0
    assert record["failure_analysis"] is None
