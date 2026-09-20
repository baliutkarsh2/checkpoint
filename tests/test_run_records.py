"""A finished run must leave a record somebody else can read.

`checkpoint runs`, the dashboard and any CI artifact all read the JSON file a
run writes as it ends. These tests pin what that file has to contain — the
score, every criterion with the evaluator that decided it, and, when it was
asked for, an explanation attached to the criterion it is about — and that the
last-run pointer really points at it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from checkpoint.runner import CriterionResult, RunResult
from checkpoint.scenario import Scenario


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


def _scenario(tmp_path: Path) -> Scenario:
    scenario = Scenario(title="cross-team launch")
    scenario.prompt = "Do a coordinated cross-team launch."
    scenario.source_path = str(tmp_path / "launch.md")
    return scenario


def _written_record(tmp_path: Path) -> dict:
    """The record the last-run pointer points at."""
    cache = (tmp_path / ".checkpoint" / "cache").resolve()
    pointer = json.loads((cache / "last-run.json").read_text())
    path = cache / "runs" / f"{pointer['run_id']}.json"
    assert path.exists(), f"the pointer names {path}, which was never written"
    return json.loads(path.read_text())


def test_a_run_is_written_where_the_last_run_pointer_says(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the cache is relative to the working directory
    from checkpoint.cli.run import _persist

    result = RunResult("done", "", 0, [], {})
    result.criteria = [CriterionResult("c1", "D", True, "ok", "deterministic")]

    _persist(result, _scenario(tmp_path), "gpt-5.6-luna", duration_ms=12.3, explain=False)

    record = _written_record(tmp_path)
    assert record["satisfaction"] == 100.0
    assert record["scenario"] == "cross-team launch"
    assert record["evaluator_model"] == "gpt-5.6-luna"
    assert record["failure_analysis"] is None


def test_an_explained_run_attaches_each_reason_to_its_own_criterion(tmp_path, monkeypatch):
    """An explanation filed under the wrong criterion is worse than none."""
    monkeypatch.chdir(tmp_path)
    import openai as openai_mod

    from checkpoint.cli.run import _persist

    result = RunResult("done", "", 0, [{"i": 0}], {"x": 1})
    result.criteria = [
        CriterionResult("c1", "D", True, "ok", "deterministic"),
        CriterionResult("c2", "D", False, "missing", "llm-json"),
    ]
    # The analyzer ids the failed criteria c0, c1, ... in the order it sent them,
    # so c0 here is the run's *second* criterion — the only failing one.
    canned = json.dumps({"analyses": [{"id": "c0", "why": "Trace entry 0 did the wrong thing."}]})
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: FakeOpenAI([canned]))

    _persist(result, _scenario(tmp_path), "gpt-4o-mini", duration_ms=1.0, explain=True)

    record = _written_record(tmp_path)
    assert record["satisfaction"] == 50.0  # 1 of 2 criteria passed
    assert record["failure_analysis"] == {"c2": "Trace entry 0 did the wrong thing."}
    assert {c["evaluator"] for c in record["criteria"]} == {"deterministic", "llm-json"}


def test_a_passing_run_never_calls_the_explainer(tmp_path, monkeypatch):
    """Nothing failed, so there is nothing to explain and no reason to pay for one."""
    monkeypatch.chdir(tmp_path)
    import openai as openai_mod

    from checkpoint.cli.run import _persist

    result = RunResult("done", "", 0, [], {})
    result.criteria = [CriterionResult("c1", "D", True, "ok", "deterministic")]
    monkeypatch.setattr(openai_mod, "OpenAI",
                        lambda: (_ for _ in ()).throw(RuntimeError("should not be called")))

    _persist(result, _scenario(tmp_path), "m", duration_ms=1.0, explain=True)

    assert _written_record(tmp_path)["failure_analysis"] is None


def test_a_failing_explainer_still_leaves_the_run_on_disk(tmp_path, monkeypatch):
    """The score is the result; an explanation is a bonus that must not cost it."""
    monkeypatch.chdir(tmp_path)
    import openai as openai_mod

    from checkpoint.cli.run import _persist

    result = RunResult("done", "", 0, [], {})
    result.criteria = [CriterionResult("c2", "D", False, "missing", "llm-json")]
    monkeypatch.setattr(openai_mod, "OpenAI",
                        lambda: (_ for _ in ()).throw(RuntimeError("the judge is down")))

    _persist(result, _scenario(tmp_path), "m", duration_ms=1.0, explain=True)

    record = _written_record(tmp_path)
    assert record["satisfaction"] == 0.0
    assert record["failure_analysis"] is None
