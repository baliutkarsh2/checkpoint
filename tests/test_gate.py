"""The statistical release gate: stats, verdict aggregation, and the CLI.

The property every test here defends is the same one: the gate must never exit 0
for an agent it has not confidently seen working. A soft verdict that returns
success is worse than no gate at all, because it is trusted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.engine import SandboxError
from checkpoint.gate import EXIT_CODES, GatePolicy, run_gate
from checkpoint.gate import engine as gate_engine
from checkpoint.gate.verdict import decide_verdict, summarize_scenario
from checkpoint.stats import classify_stability, runs_needed, wilson_interval, z_for

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "examples" / "smoke" / "smoke-scenario.md"
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"

SCENARIO_MD = "# s\n## Prompt\ndo\n## Success Criteria\n- [D] x exists\n## Config\nclones: github\n"


# --- Wilson interval --------------------------------------------------------


class _NullSandbox:
    """Stands in for a real sandbox when the per-run function is stubbed."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def _stub_runs(monkeypatch, fn):
    monkeypatch.setattr(gate_engine, "Sandbox", _NullSandbox)
    monkeypatch.setattr(gate_engine, "run_scenario", fn)

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


# --- exact z ----------------------------------------------------------------

def test_z_is_exact_for_untabulated_confidence():
    # The old lookup table snapped 0.85 to the 0.80 entry, so asking for a wider
    # interval silently produced a narrower one.
    assert z_for(0.95) == pytest.approx(1.959964, abs=1e-6)
    assert z_for(0.85) == pytest.approx(1.439531, abs=1e-6)
    assert z_for(0.80) == pytest.approx(1.281552, abs=1e-6)
    assert z_for(0.85) > z_for(0.80)


def test_z_rejects_impossible_confidence():
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            z_for(bad)


def test_interval_widens_with_confidence():
    widths = [wilson_interval(8, 10, c).width for c in (0.80, 0.85, 0.90, 0.95, 0.99)]
    assert all(a < b for a, b in zip(widths, widths[1:], strict=False))


# --- how many runs SHIP needs ----------------------------------------------

def test_runs_needed_matches_the_interval_it_documents():
    for ship_min in (0.5, 0.7, 0.8, 0.9, 0.95):
        n = runs_needed(ship_min, 0.95)
        assert wilson_interval(n, n, 0.95).low >= ship_min
        assert wilson_interval(n - 1, n - 1, 0.95).low < ship_min


def test_runs_needed_default_policy():
    # 16 flawless runs at ship_min 0.80 / 95% confidence. At n=5 (the number a
    # user reaches for first) SHIP is not merely unlikely, it is unreachable.
    assert runs_needed(0.80, 0.95) == 16
    assert GatePolicy(runs=5).min_runs_to_ship == 16


def test_runs_needed_rejects_a_perfect_ship_min():
    with pytest.raises(ValueError):
        runs_needed(1.0, 0.95)


# --- classification ---------------------------------------------------------

def test_classify_stable_pass_and_fail_and_flaky():
    assert classify_stability(wilson_interval(20, 20), ship_min=0.80, block_max=0.50) == "stable_pass"
    assert classify_stability(wilson_interval(0, 20), ship_min=0.80, block_max=0.50) == "stable_fail"
    assert classify_stability(wilson_interval(10, 20), ship_min=0.80, block_max=0.50) == "flaky"


def test_classify_every_run_failed_is_a_decision_at_any_n():
    # 0/3 used to be "flaky" (Wilson upper bound 0.56 > block_max 0.50) and
    # therefore CONDITIONAL and therefore green. An agent that failed every run
    # has decided the question.
    for n in (1, 2, 3, 5):
        assert classify_stability(wilson_interval(0, n), ship_min=0.80, block_max=0.50) == "stable_fail"


def test_classify_underpowered_run_is_inconclusive_not_flaky():
    assert classify_stability(wilson_interval(5, 5), ship_min=0.80, block_max=0.50) == "inconclusive"
    assert classify_stability(wilson_interval(3, 3), ship_min=0.80, block_max=0.50) == "inconclusive"
    # With enough runs the same straddling interval is a real "flaky" finding.
    assert classify_stability(wilson_interval(16, 20), ship_min=0.80, block_max=0.50) == "flaky"


def test_classify_no_samples_is_an_error():
    assert classify_stability(wilson_interval(0, 0), ship_min=0.80, block_max=0.50) == "error"


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


def test_conditional_is_not_success_by_default():
    # The headline bug: CONDITIONAL used to exit 0.
    p = GatePolicy(runs=20)
    flaky = _stat("c", [100.0] * 10 + [0.0] * 10, p)
    verdict, code = decide_verdict([flaky], p)
    assert verdict == "CONDITIONAL" and code == EXIT_CODES["CONDITIONAL"] != 0


def test_allow_conditional_opts_into_a_green_conditional():
    p = GatePolicy(runs=20, allow_conditional=True)
    flaky = _stat("c", [100.0] * 10 + [0.0] * 10, p)
    assert decide_verdict([flaky], p) == ("CONDITIONAL", 0)


def test_strict_overrides_allow_conditional():
    # A release pipeline can tighten a shared config that opted in.
    p = GatePolicy(runs=20, allow_conditional=True, strict=True)
    flaky = _stat("c", [100.0] * 10 + [0.0] * 10, p)
    assert decide_verdict([flaky], p) == ("CONDITIONAL", EXIT_CODES["CONDITIONAL"])


def test_allow_conditional_never_greens_a_worse_verdict():
    p = GatePolicy(runs=20, allow_conditional=True)
    block = _stat("b", [0.0] * 20, p)
    inconclusive = _stat("i", [100.0] * 5, p)
    assert decide_verdict([block], p) == ("BLOCK", EXIT_CODES["BLOCK"])
    assert decide_verdict([inconclusive], p) == ("INCONCLUSIVE", EXIT_CODES["INCONCLUSIVE"])
    assert decide_verdict([], p) == ("ERROR", EXIT_CODES["ERROR"])


def test_worst_scenario_decides_the_verdict():
    p = GatePolicy(runs=20)
    ship = _stat("a", [100.0] * 20, p)
    inconclusive = summarize_scenario("b", [100.0] * 5, [True] * 5, p)
    block = _stat("c", [0.0] * 20, p)
    assert decide_verdict([ship, inconclusive], p)[0] == "INCONCLUSIVE"
    assert decide_verdict([ship, inconclusive, block], p)[0] == "BLOCK"


def test_incomplete_run_counts_as_failure():
    p = GatePolicy(runs=4)
    # Two complete passes, two crashed (complete=False) => 2/4 passes.
    stat = summarize_scenario("x", [100.0, 100.0, 0.0, 0.0], [True, True, False, False], p)
    assert stat.passes == 2


def test_inconclusive_stat_says_how_many_runs_it_needs():
    stat = summarize_scenario("x", [100.0] * 5, [True] * 5, GatePolicy(runs=5))
    assert stat.classification == "inconclusive"
    assert stat.min_runs == 16
    assert "SHIP needs >= 16 clean runs at ship_min 0.80" in stat.evidence()


def test_policy_rejects_contradictory_thresholds():
    with pytest.raises(ValueError):
        GatePolicy(ship_min=0.5, block_max=0.6)
    with pytest.raises(ValueError):
        GatePolicy(confidence=1.0)
    with pytest.raises(ValueError):
        GatePolicy(runs=0)


# --- exit-code table --------------------------------------------------------

@pytest.mark.parametrize("n", [1, 3, 5, 20])
@pytest.mark.parametrize("shape", ["all-fail", "half-pass", "all-pass"])
def test_exit_code_table(n, shape):
    """Every combination of outcome and N, pinned. Only a confident pass is 0."""
    passes = {"all-fail": 0, "half-pass": n // 2, "all-pass": n}[shape]
    p = GatePolicy(runs=n)
    stat = _stat("s", [100.0] * passes + [0.0] * (n - passes), p)
    verdict, code = decide_verdict([stat], p)

    if passes == 0:
        expected = "BLOCK"                       # never a green build for 0 passes
    elif n < p.min_runs_to_ship:
        expected = "INCONCLUSIVE"                # too few runs to decide anything
    elif passes == n:
        expected = "SHIP"
    else:
        expected = "CONDITIONAL"
    assert (verdict, code) == (expected, EXIT_CODES[expected])
    assert (code == 0) == (expected == "SHIP")


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
    scn.write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20))
    assert res.verdict == "SHIP" and res.exit_code == 0
    assert res.scenarios[0].passes == 20


def test_run_gate_block_with_stubbed_runner(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(0.0))
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20))
    assert res.verdict == "BLOCK" and res.exit_code == 1


def test_run_gate_zero_passes_at_small_n_blocks(tmp_path, monkeypatch):
    """0/3 used to be CONDITIONAL, exit 0 — a green build for a dead agent."""
    scn = tmp_path / "s.md"
    scn.write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(0.0))
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=3))
    assert res.verdict == "BLOCK" and res.exit_code != 0


def test_run_gate_skips_files_that_are_not_scenarios(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# How to use these scenarios\n\nSome prose.\n")
    (tmp_path / "notes.md").write_text("# Notes\n## Prompt\ndo something\n")  # no criteria
    (tmp_path / "a.md").write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))

    res = run_gate(tmp_path, ["python", "x.py"], GatePolicy(runs=20))
    assert [s.scenario for s in res.scenarios] == ["a.md"]
    skipped = {s.path: s.reason for s in res.skipped}
    assert set(skipped) == {"README.md", "notes.md"}
    assert "not a scenario" in skipped["README.md"]
    assert "nothing to score" in skipped["notes.md"]
    assert res.verdict == "SHIP"


def test_run_gate_errors_when_target_matches_no_scenario(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Just docs\n")
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))
    res = run_gate(tmp_path, ["python", "x.py"], GatePolicy(runs=5))
    assert res.verdict == "ERROR" and res.exit_code == EXIT_CODES["ERROR"]
    assert any("no runnable scenario" in e for e in res.errors)


def test_scenarios_are_keyed_by_path_not_file_name(tmp_path, monkeypatch):
    for team in ("github", "slack"):
        (tmp_path / team).mkdir()
        (tmp_path / team / "smoke.md").write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))
    res = run_gate(tmp_path, ["python", "x.py"], GatePolicy(runs=20))
    assert sorted(s.scenario for s in res.scenarios) == ["github/smoke.md", "slack/smoke.md"]


# --- infrastructure failures are never agent failures ----------------------

class _BrokenSandbox:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise SandboxError("could not start the github twin: docker is not running")

    def stop(self):
        pass


def test_sandbox_failure_is_an_error_not_a_failed_agent(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text(SCENARIO_MD)
    monkeypatch.setattr(gate_engine, "Sandbox", _BrokenSandbox)
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=5))

    stat = res.scenarios[0]
    assert res.verdict == "ERROR" and res.exit_code == EXIT_CODES["ERROR"] != 0
    assert stat.classification == "error"
    assert (stat.n, stat.passes, stat.error_runs) == (0, 0, 5)
    assert any("docker is not running" in r for r in stat.error_reasons)


def test_sandbox_failure_exits_nonzero_with_an_error_in_the_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(SCENARIO_MD)
    monkeypatch.setattr(gate_engine, "Sandbox", _BrokenSandbox)

    r = CliRunner().invoke(main, [
        "gate", str(scn_dir), "--harness", "python agent.py", "-n", "3", "-o", "json",
    ])
    assert r.exit_code != 0
    payload = json.loads(r.output)
    assert payload["verdict"] == "ERROR"
    assert payload["scenarios"][0]["classification"] == "error"
    assert payload["scenarios"][0]["error_runs"] == 3
    assert any("docker is not running" in e for e in payload["errors"])


def test_partial_sandbox_failures_do_not_count_as_agent_failures(tmp_path, monkeypatch):
    """A flaky sandbox shrinks the sample; it must not manufacture failures."""
    scn = tmp_path / "s.md"
    scn.write_text(SCENARIO_MD)
    calls = {"n": 0}

    def _run(*a, **k):
        calls["n"] += 1
        if calls["n"] % 4 == 0:
            result = _FakeResult(0.0, complete=False, error="sandbox setup failed: boom")
            result.setup_error = True
            return result
        return _FakeResult(100.0)

    _stub_runs(monkeypatch, _run)
    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20))
    stat = res.scenarios[0]
    assert stat.error_runs == 5
    assert (stat.n, stat.passes) == (15, 15)      # errored runs left the denominator
    assert stat.pass_rate == 1.0


def test_judge_credential_failure_is_an_error(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text("# s\n## Prompt\ndo\n## Success Criteria\n- [P] the tone is friendly\n")
    for key in ("OPENAI_API_KEY", "CHECKPOINT_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    ran: list[int] = []

    def _run(*a, **k):
        ran.append(1)
        return _FakeResult(100.0)

    _stub_runs(monkeypatch, _run)

    res = run_gate(scn, ["python", "x.py"], GatePolicy(runs=20), judge_model="gpt-4o-mini")
    assert res.verdict == "ERROR" and res.exit_code == EXIT_CODES["ERROR"]
    assert any("OPENAI_API_KEY" in e for e in res.errors)
    assert not ran, "the gate must refuse before burning runs it cannot score"


def test_judge_credential_check_ignores_deterministic_scenarios(tmp_path, monkeypatch):
    scn = tmp_path / "s.md"
    scn.write_text(SCENARIO_MD)  # [D] only — no judge needed
    for key in ("OPENAI_API_KEY", "CHECKPOINT_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))
    assert run_gate(scn, ["python", "x.py"], GatePolicy(runs=20)).verdict == "SHIP"


# --- CLI end-to-end (real subprocess runs, deterministic scenario) ----------

def test_gate_cli_end_to_end(tmp_path, monkeypatch):
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
        pytest.skip("smoke assets missing")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(main, [
        "gate", str(SMOKE),
        "--harness", f"{sys.executable} {FAKE_HARNESS}",
        "-n", "5", "-o", "json",
    ])
    # A flawless 5/5 cannot clear ship_min 0.80: the gate says so and exits
    # non-zero instead of passing the build off as "conditional".
    assert result.exit_code == EXIT_CODES["INCONCLUSIVE"], result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "INCONCLUSIVE"
    scenario = payload["scenarios"][0]
    assert scenario["passes"] == 5
    assert scenario["mean_score"] == 100.0
    assert scenario["classification"] == "inconclusive"
    assert scenario["runs_needed_to_ship"] == 16
    assert "SHIP needs >= 16 clean runs" in scenario["evidence"]


def test_gate_cli_confidence_uses_the_exact_z(tmp_path, monkeypatch):
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
        pytest.skip("smoke assets missing")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(main, [
        "gate", str(SMOKE),
        "--harness", f"{sys.executable} {FAKE_HARNESS}",
        "-n", "3", "--confidence", "0.85", "-o", "json",
    ])
    payload = json.loads(result.output)
    low = payload["scenarios"][0]["ci_low"]
    assert low == pytest.approx(wilson_interval(3, 3, 0.85).low, abs=5e-5)
    # ...and NOT the 0.80 value the old lookup table would have snapped to.
    assert low != pytest.approx(wilson_interval(3, 3, 0.80).low, abs=5e-5)


def test_gate_cli_rejects_an_impossible_policy(tmp_path):
    result = CliRunner().invoke(main, [
        "gate", str(SMOKE), "--harness", "python a.py", "--ship-min", "1.0",
    ])
    assert result.exit_code != 0
    assert "ship_min" in (result.output + getattr(result, "stderr", ""))


def test_gate_cli_readme_in_the_scenario_dir_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "README.md").write_text("# Scenario library\n\nHow to write these.\n")
    (scn_dir / "a.md").write_text(SCENARIO_MD)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(100.0))

    r = CliRunner().invoke(main, [
        "gate", str(scn_dir), "--harness", "python agent.py", "-n", "20", "-o", "json",
    ])
    payload = json.loads(r.output)
    assert [s["scenario"] for s in payload["scenarios"]] == ["a.md"]
    assert [s["path"] for s in payload["skipped"]] == ["README.md"]
    assert r.exit_code == 0 and payload["verdict"] == "SHIP"


def test_gate_cli_allow_conditional_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(SCENARIO_MD)
    scores = iter([100.0] * 16 + [0.0] * 4)
    _stub_runs(monkeypatch, lambda *a, **k: _FakeResult(next(scores)))

    args = ["gate", str(scn_dir), "--harness", "python agent.py", "-n", "20", "-o", "json"]
    r = CliRunner().invoke(main, args)
    assert json.loads(r.output)["verdict"] == "CONDITIONAL"
    assert r.exit_code == EXIT_CODES["CONDITIONAL"]

    scores = iter([100.0] * 16 + [0.0] * 4)
    r = CliRunner().invoke(main, [*args, "--allow-conditional"])
    assert r.exit_code == 0 and json.loads(r.output)["verdict"] == "CONDITIONAL"

    scores = iter([100.0] * 16 + [0.0] * 4)
    r = CliRunner().invoke(main, [*args, "--allow-conditional", "--strict"])
    assert r.exit_code == EXIT_CODES["CONDITIONAL"]


def test_gate_writes_and_verifies_certificate(tmp_path, monkeypatch):
    if not SMOKE.is_file() or not FAKE_HARNESS.is_file():
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
    # Three runs cannot support a SHIP, but the evidence is still certifiable:
    # the certificate records the honest verdict, not a convenient one.
    assert r.exit_code == EXIT_CODES["INCONCLUSIVE"], r.output
    assert cert_file.is_file()
    cert_doc = json.loads(cert_file.read_text())
    assert cert_doc["subject"]["agent"] == "smoke-bot"
    assert cert_doc["verdict"] == "INCONCLUSIVE"
    assert "signature" in cert_doc

    # The `cert verify` command accepts the freshly written certificate.
    v = CliRunner().invoke(main, ["cert", "verify", str(cert_file)])
    assert v.exit_code == 0, v.output
    assert "VALID" in v.output
