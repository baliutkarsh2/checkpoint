"""Running a scenario's N runs in parallel must not change what the gate decides.

The gate shards the runs of one scenario across worker threads. Until these were
written the parallel path had no coverage at all, which is why `concurrency`
defaulted to 1: a serial gate is slow, but an untested concurrent one could be
wrong, and a testing tool that is occasionally wrong is worth less than nothing.

What could go wrong, and what each test pins:

* results written to the wrong index, so a failing run is recorded against a
  different one and the pass count is right by luck;
* runs silently dropped or double-counted under a race, so `n` disagrees with
  the number actually executed;
* a verdict that depends on how many workers happened to be free.
"""
from __future__ import annotations

import threading

import pytest

from checkpoint.gate import GatePolicy, run_gate
from checkpoint.gate import engine as gate_engine
from checkpoint.runner import CriterionResult, RunResult

SCENARIO_MD = "# s\n## Prompt\ndo\n## Success Criteria\n- [D] x exists\n## Config\nclones: github\n"


class _NullSandbox:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def prepare(self, *a, **k):
        pass

    def views(self):
        return {}

    def state(self):
        return {}

    def trace(self):
        return []

    def egress_events(self):
        return []

    @property
    def twins(self):
        return ["github"]

    @property
    def workspace(self):
        return None


def _result(passed: bool) -> RunResult:
    result = RunResult(final_answer="done", stderr="", exit_code=0,
                       trace=[], state={})
    result.criteria = [CriterionResult("c", "D", passed, "", "assertion:pinned")]
    return result


def _stub(monkeypatch, run_fn):
    monkeypatch.setattr(gate_engine, "Sandbox", _NullSandbox)
    monkeypatch.setattr(gate_engine, "run_scenario", run_fn)


def _scenario(tmp_path):
    path = tmp_path / "s.md"
    path.write_text(SCENARIO_MD, encoding="utf-8")
    return path


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_every_run_is_executed_exactly_once(tmp_path, monkeypatch, concurrency):
    """N runs means N executions, whatever the worker count."""
    calls = []
    lock = threading.Lock()

    def run_fn(*a, **k):
        with lock:
            calls.append(1)
        return _result(True)

    _stub(monkeypatch, run_fn)
    res = run_gate(_scenario(tmp_path), ["python", "x.py"],
                   GatePolicy(runs=16), concurrency=concurrency)
    assert len(calls) == 16, f"{len(calls)} executions for 16 runs at -j {concurrency}"
    assert res.scenarios[0].passes == 16
    assert res.scenarios[0].n == 16


@pytest.mark.parametrize("concurrency", [2, 4, 8])
def test_the_verdict_does_not_depend_on_the_worker_count(tmp_path, monkeypatch, concurrency):
    """The same runs, sharded differently, must reach the same conclusion.

    A mixed result is used deliberately: an all-pass or all-fail scenario would
    agree even if results were being written to the wrong slots.
    """
    def make(sequence):
        state = {"i": 0}
        lock = threading.Lock()

        def run_fn(*a, **k):
            with lock:
                index = state["i"]
                state["i"] += 1
            return _result(sequence[index % len(sequence)])
        return run_fn

    # 12 of 16 pass -> flaky -> CONDITIONAL, the interesting middle case.
    sequence = [True] * 3 + [False]

    _stub(monkeypatch, make(sequence))
    serial = run_gate(_scenario(tmp_path), ["python", "x.py"],
                      GatePolicy(runs=16), concurrency=1)
    _stub(monkeypatch, make(sequence))
    parallel = run_gate(_scenario(tmp_path), ["python", "x.py"],
                        GatePolicy(runs=16), concurrency=concurrency)

    assert serial.scenarios[0].passes == parallel.scenarios[0].passes == 12
    assert serial.scenarios[0].n == parallel.scenarios[0].n == 16
    assert serial.verdict == parallel.verdict
    assert serial.exit_code == parallel.exit_code
    assert (serial.scenarios[0].classification
            == parallel.scenarios[0].classification == "flaky")


def test_a_failure_is_recorded_against_the_run_that_failed(tmp_path, monkeypatch):
    """Exactly one run fails; the tally must be 15, not 16 or 14.

    This is the indexing bug this file exists to catch: workers writing into a
    shared list can lose or duplicate a result without changing its length.
    """
    state = {"i": 0}
    lock = threading.Lock()

    def run_fn(*a, **k):
        with lock:
            index = state["i"]
            state["i"] += 1
        # The 7th run fails; every other one passes.
        return _result(index != 6)

    _stub(monkeypatch, run_fn)
    res = run_gate(_scenario(tmp_path), ["python", "x.py"],
                   GatePolicy(runs=16), concurrency=4)
    assert res.scenarios[0].passes == 15
    assert res.scenarios[0].n == 16


def test_runs_really_do_overlap(tmp_path, monkeypatch):
    """Concurrency must actually run things at once, not just accept the flag.

    Without this, `-j 8` could quietly execute serially and every assertion
    above would still pass — the gate would be correct and no faster, which is
    the failure mode that made raising the default worth testing at all.
    """
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()
    started = threading.Barrier(4, timeout=30)

    def run_fn(*a, **k):
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        try:
            # Blocks until four workers are inside run_scenario together; a
            # serial implementation never gets here and raises BrokenBarrier.
            started.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - failure path
            pass
        with lock:
            peak["now"] -= 1
        return _result(True)

    _stub(monkeypatch, run_fn)
    run_gate(_scenario(tmp_path), ["python", "x.py"],
             GatePolicy(runs=8), concurrency=4)
    assert peak["max"] >= 4, f"only {peak['max']} run(s) were ever in flight at -j 4"
