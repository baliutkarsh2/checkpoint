"""Phase 5 / Plan 05-03: run-record persistence tests."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from checkpoint.run_record import (
    build_record,
    load_last_run,
    make_run_id,
    write_record,
)


@dataclass
class _C:
    text: str
    kind: str
    passed: bool
    reasoning: str
    evaluator: str


def test_run_id_is_stable_with_same_inputs():
    a = make_run_id("scenarios/a.md", "2026-05-12T12:00:00Z")
    b = make_run_id("scenarios/a.md", "2026-05-12T12:00:00Z")
    assert a == b
    assert len(a) == 12


def test_run_id_differs_per_scenario():
    a = make_run_id("scenarios/a.md", "2026-05-12T12:00:00Z")
    b = make_run_id("scenarios/b.md", "2026-05-12T12:00:00Z")
    assert a != b


def test_run_id_differs_per_timestamp():
    a = make_run_id("scenarios/a.md", "2026-05-12T12:00:00Z")
    b = make_run_id("scenarios/a.md", "2026-05-12T12:00:01Z")
    assert a != b


def test_build_record_satisfaction_and_criteria():
    rec = build_record(
        scenario_name="demo",
        scenario_path="/abs/demo.md",
        satisfaction=75.0,
        criteria=[
            _C("c1", "D", True, "ok", "deterministic"),
            _C("c2", "P", False, "no", "llm"),
        ],
        evaluator_model="gpt-4o-mini",
        evaluator_model_source="default",
        final_answer="answer",
        trace=[{"i": 1}],
        state={"issues": {}},
    )
    assert rec["satisfaction"] == 75.0
    assert len(rec["criteria"]) == 2
    assert rec["criteria"][0]["text"] == "c1"
    assert rec["evaluator_model_source"] == "default"
    assert rec["env"]["timestamp"]
    assert rec["env"]["cli_version"]


def test_state_truncated_when_huge():
    huge = {f"key{i}": "x" * 1000 for i in range(500)}
    rec = build_record(
        scenario_name="big", scenario_path=None, satisfaction=100.0,
        criteria=[], evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state=huge,
    )
    assert rec["state"].get("_truncated") is True


def test_write_record_and_pointer(tmp_path: Path):
    rec = build_record(
        scenario_name="demo", scenario_path="/abs/demo.md", satisfaction=100.0,
        criteria=[_C("c1", "D", True, "ok", "deterministic")],
        evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state={},
    )
    path = write_record(rec, root=tmp_path)
    assert path.exists()
    assert path.parent.name == "runs"

    pointer = tmp_path.resolve() / "last-run.json"
    assert pointer.exists()
    ptr = json.loads(pointer.read_text())
    assert ptr["run_id"] == rec["run_id"]


def test_load_last_run_round_trip(tmp_path: Path):
    rec = build_record(
        scenario_name="demo", scenario_path=None, satisfaction=80.0,
        criteria=[], evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state={},
    )
    write_record(rec, root=tmp_path)
    loaded = load_last_run(root=tmp_path)
    assert loaded is not None
    assert loaded["run_id"] == rec["run_id"]
    assert loaded["satisfaction"] == 80.0


def test_failure_analysis_passthrough():
    rec = build_record(
        scenario_name="demo", scenario_path=None, satisfaction=50.0,
        criteria=[_C("c", "D", False, "missing", "llm-json")],
        evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state={},
        failure_analysis={"c": "Trace entry 3 created the wrong title."},
    )
    assert rec["failure_analysis"] == {"c": "Trace entry 3 created the wrong title."}


def test_no_failure_analysis_when_none():
    rec = build_record(
        scenario_name="demo", scenario_path=None, satisfaction=100.0,
        criteria=[], evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state={},
        failure_analysis=None,
    )
    assert rec["failure_analysis"] is None


def test_writes_under_runs_directory(tmp_path: Path):
    rec = build_record(
        scenario_name="x", scenario_path=None, satisfaction=100.0,
        criteria=[], evaluator_model="m", evaluator_model_source="default",
        final_answer="", trace=[], state={},
    )
    path = write_record(rec, root=tmp_path)
    rel = path.relative_to(tmp_path.resolve())
    parts = list(rel.parts)
    assert parts[0] == "runs"
    assert parts[1].endswith(".json")
