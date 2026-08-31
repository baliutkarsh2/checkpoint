"""Persistent baselines turn a pass-rate drop into a `regression` verdict."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.gate import baseline, run_gate
from checkpoint.gate import engine as gate_engine
from checkpoint.gate.verdict import GatePolicy, summarize_scenario


class _FakeResult:
    def __init__(self, score):
        self._score = score
        self.complete = True
        self.error = None

    @property
    def score(self):
        return self._score


_SCN = "# s\n## Prompt\np\n## Success Criteria\n- [D] x\n## Config\nclones: github\n"


def test_baseline_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    target = tmp_path / "scenarios"
    policy = GatePolicy(runs=20)
    stats = [summarize_scenario("a.md", [100.0] * 20, [True] * 20, policy)]
    baseline.save(target, stats)
    assert baseline.baseline_path().exists()
    loaded = baseline.load(target)
    assert loaded["a.md"] == 1.0


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    assert baseline.load(tmp_path / "nope") == {}


def test_run_gate_flags_regression_with_baseline(tmp_path, monkeypatch):
    scn = tmp_path / "a.md"
    scn.write_text(_SCN)
    # Now the agent fails everything; baseline says it used to pass ~95%.
    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _FakeResult(0.0))
    result = run_gate(scn, ["python", "x"], GatePolicy(runs=20), baselines={"a.md": 0.95})
    assert result.scenarios[0].classification == "regression"
    assert result.verdict == "BLOCK"


def test_gate_cli_writes_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)
    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _FakeResult(100.0))

    r = CliRunner().invoke(main, [
        "gate", str(scn_dir), "--harness", "python agent.py", "-n", "20", "-o", "json",
    ])
    assert r.exit_code == 0, r.output
    data = json.loads(baseline.baseline_path().read_text())
    # One target section, with a.md recorded at pass_rate 1.0.
    section = next(iter(data.values()))
    assert section["a.md"]["pass_rate"] == 1.0


def test_gate_cli_no_baseline_flag_skips_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)
    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _FakeResult(100.0))

    r = CliRunner().invoke(main, [
        "gate", str(scn_dir), "--harness", "python agent.py", "-n", "5",
        "--no-baseline", "-o", "json",
    ])
    assert r.exit_code == 0, r.output
    assert not baseline.baseline_path().exists()
