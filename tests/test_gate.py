"""The statistical release gate: stats, verdict aggregation, and the CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.gate import GatePolicy, run_gate
from checkpoint.gate import engine as gate_engine
from checkpoint.gate.verdict import decide_verdict, summarize_scenario
from checkpoint.stats import classify_stability, wilson_interval

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "examples" / "smoke" / "smoke-scenario.md"
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"


# --- Wilson interval --------------------------------------------------------

def test_wilson_bounds_in_unit_interval():
    for passes, n in [(0, 5), (5, 5), (3, 10), (1, 100)]:
        ci = wilson_interval(passes, n)
        assert 0.0 <= ci.low <= ci.high <= 1.0
        assert abs(ci.point - passes / n) < 1e-9


def test_wilson_perfect_run_lower_bound_grows_with_n():
    # More perfect runs => more confidence => higher lower bound.
    assert wilson_interval(5, 5).low < wilson_interval(20, 20).low < wilson_interval(100, 100).low


def test_wilson_zero_passes_upper_bound_shrinks_with_n():
    assert wilson_interval(0, 5).high > wilson_interval(0, 50).high


# --- classification ---------------------------------------------------------

def test_classify_stable_pass_and_fail_and_flaky():
    assert classify_stability(wilson_interval(20, 20), ship_min=0.80, block_max=0.50) == "stable_pass"
    assert classify_stability(wilson_interval(0, 20), ship_min=0.80, block_max=0.50) == "stable_fail"
    assert classify_stability(wilson_interval(10, 20), ship_min=0.80, block_max=0.50) == "flaky"


def test_classify_regression_against_baseline():
    ci = wilson_interval(10, 20)  # pass rate 0.5, was 0.95 before
    assert classify_stability(ci, baseline_rate=0.95, regression_drop=0.20) == "regression"


# --- verdict aggregation ----------------------------------------------------

def _stat(name, scores, policy, baseline=None):
    return summarize_scenario(name, scores, [True] * len(scores), policy, baseline_rate=baseline)


def test_verdict_ship_block_conditional():
    p = GatePolicy(runs=20)
    ship = _stat("a", [100.0] * 20, p)
    block = _stat("b", [0.0] * 20, p)
    flaky = _stat("c", [100.0] * 10 + [0.0] * 10, p)
    assert decide_verdict([ship], p)[0] == "SHIP"
    assert decide_verdict([ship, block], p)[0] == "BLOCK"
    assert decide_verdict([ship, flaky], p)[0] == "CONDITIONAL"


def test_strict_conditional_exits_nonzero():
    p = GatePolicy(runs=20, strict=True)
    flaky = _stat("c", [100.0] * 10 + [0.0] * 10, p)
    verdict, code = decide_verdict([flaky], p)
    assert verdict == "CONDITIONAL" and code == 1


def test_incomplete_run_counts_as_failure():
    p = GatePolicy(runs=4)
    # Two complete passes, two crashed (complete=False) => 2/4 passes.
    stat = summarize_scenario("x", [100.0, 100.0, 0.0, 0.0], [True, True, False, False], p)
    assert stat.passes == 2


# --- engine with a stubbed runner (fast, deterministic) ---------------------

class _FakeResult:
    def __init__(self, score, complete=True, error=None):
        self._score = score
        self.complete = complete
        self.error = error

    @property
    def score(self):
        return self._score


def test_run_gate_ship_with_stubbed_runner(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text("# s\n## Prompt\ndo\n## Success Criteria\n- [D] x exists\n## Config\nclones: github\n")
    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _FakeResult(100.0))
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20))
    assert res.verdict == "SHIP" and res.exit_code == 0
    assert res.scenarios[0].passes == 20


def test_run_gate_block_with_stubbed_runner(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text("# s\n## Prompt\ndo\n## Success Criteria\n- [D] x exists\n## Config\nclones: github\n")
    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _FakeResult(0.0))
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20))
    assert res.verdict == "BLOCK" and res.exit_code == 1


# --- CLI end-to-end (real subprocess runs, deterministic scenario) ----------

def test_gate_cli_end_to_end(tmp_path, monkeypatch):
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
        import pytest
        pytest.skip("smoke assets missing")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(main, [
        "gate", str(SMOKE),
        "--harness", f"{sys.executable} {FAKE_HARNESS}",
        "-n", "5", "-o", "json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] in ("SHIP", "CONDITIONAL")
    assert payload["scenarios"][0]["passes"] == 5
    assert payload["scenarios"][0]["mean_score"] == 100.0


def test_gate_writes_and_verifies_certificate(tmp_path, monkeypatch):
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
        import pytest
        pytest.skip("smoke assets missing")
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))  # isolate the signing key
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cert_file = tmp_path / "cert.json"

    r = CliRunner().invoke(main, [
        "gate", str(SMOKE),
        "--harness", f"{sys.executable} {FAKE_HARNESS}",
        "-n", "3", "--agent", "smoke-bot",
        "--certificate", str(cert_file), "-o", "json",
    ])
    assert r.exit_code == 0, r.output
    assert cert_file.is_file()
    cert_doc = json.loads(cert_file.read_text())
    assert cert_doc["subject"]["agent"] == "smoke-bot"
    assert "signature" in cert_doc

    # The `cert verify` command accepts the freshly written certificate.
    v = CliRunner().invoke(main, ["cert", "verify", str(cert_file)])
    assert v.exit_code == 0, v.output
    assert "VALID" in v.output
