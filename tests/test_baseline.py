"""Persistent baselines turn a pass-rate drop into a `regression` verdict.

The baseline is the only memory the gate has. Every test here pins a way it used
to forget: a ledger that followed a degrading agent downward, keys that collided
across directories, a file keyed by an absolute path that CI could never find,
and a comparison that called noise a regression (or missed a real one by a
floating-point hair).
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.gate import EXIT_CODES, baseline, run_gate
from checkpoint.gate import engine as gate_engine
from checkpoint.gate.baseline import Baseline
from checkpoint.gate.verdict import GatePolicy, summarize_scenario
from checkpoint.runner import CriterionResult, RunResult
from checkpoint.scenario import parse
from checkpoint.stats import is_regression, wilson_interval


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

def _run_result(score):
    """A completed run that scored ``score``, built from the real RunResult.

    Deriving the score from real criteria rather than stubbing the attribute
    keeps these tests honest about what the gate reads off a run.
    """
    passing = round(score / 100 * 10)
    result = RunResult(final_answer="done", stderr="", exit_code=0, trace=[], state={})
    result.criteria = [CriterionResult(f"c{i}", "D", i < passing, "", "assertion:pinned")
                       for i in range(10)]
    return result


_SCN = "# s\n## Prompt\np\n## Success Criteria\n- [D] x\n## Config\nclones: github\n"
_SCN_REWRITTEN = "# s\n## Prompt\np\n## Success Criteria\n- [D] y\n## Config\nclones: github\n"


def _stat(name, passes, n, policy, baseline_rate=None, scenario_md=_SCN):
    scores = [100.0] * passes + [0.0] * (n - passes)
    return summarize_scenario(
        name, scores, [True] * n, policy, baseline_rate=baseline_rate,
        criteria_hash=baseline.criteria_hash(parse(scenario_md)),
    )


def _gate_once(scn_dir, passes, n, monkeypatch, *, extra_args=()):
    """Run the gate CLI once with `passes` of `n` runs passing; return the JSON."""
    scores = iter([100.0] * passes + [0.0] * (n - passes))
    _stub_runs(monkeypatch, lambda *a, **k: _run_result(next(scores)))
    r = CliRunner().invoke(main, [
        "gate", str(scn_dir), "--command", "python agent.py",
        "-n", str(n), "--json", *extra_args,
    ])
    return r, json.loads(r.output)


def test_baseline_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    target = tmp_path / "scenarios"
    policy = GatePolicy(runs=20)
    stats = [_stat("a.md", 20, 20, policy)]
    baseline.save(target, stats)
    assert baseline.baseline_path().exists()
    loaded = baseline.load(target)
    assert loaded["a.md"].pass_rate == 1.0
    assert loaded["a.md"].criteria_hash == baseline.criteria_hash(parse(_SCN))


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    assert baseline.load(tmp_path / "nope") == {}


def test_only_a_confident_pass_is_recorded(tmp_path, monkeypatch):
    """save() refuses to ratchet the bar downward, whatever the caller asks."""
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    target = tmp_path / "scenarios"
    policy = GatePolicy(runs=20)
    baseline.save(target, [_stat("a.md", 20, 20, policy)])
    written = baseline.save(target, [_stat("a.md", 14, 20, policy)])  # flaky
    assert written == []
    assert baseline.load(target)["a.md"].pass_rate == 1.0


def test_keys_are_relative_paths_not_file_names(tmp_path, monkeypatch):
    """github/smoke.md and slack/smoke.md used to overwrite each other."""
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    target = tmp_path / "scenarios"
    policy = GatePolicy(runs=20)
    baseline.save(target, [
        _stat("github/smoke.md", 20, 20, policy),
        _stat("slack/smoke.md", 20, 20, policy),
    ])
    assert set(baseline.load(target)) == {"github/smoke.md", "slack/smoke.md"}


def test_baseline_is_found_from_a_different_checkout_path(tmp_path, monkeypatch):
    """CI checks out at another absolute path; the ledger must still match.

    The old key was a hash of the target's *absolute* path, so a baseline
    written on a laptop was invisible to every CI run.
    """
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path / "store"))
    policy = GatePolicy(runs=20)
    for checkout in ("build-1", "build-2"):
        root = tmp_path / checkout
        (root / "scenarios").mkdir(parents=True)
        monkeypatch.chdir(root)
        if checkout == "build-1":
            baseline.save(root / "scenarios", [_stat("a.md", 20, 20, policy)])
        else:
            assert baseline.load(root / "scenarios")["a.md"].pass_rate == 1.0


def test_changed_criteria_invalidate_the_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    scn = tmp_path / "a.md"
    scn.write_text(_SCN_REWRITTEN)
    stale = Baseline(pass_rate=0.95, criteria_hash=baseline.criteria_hash(parse(_SCN)))
    _stub_runs(monkeypatch, lambda *a, **k: _run_result(0.0))

    result = run_gate(scn, ["python", "x"], GatePolicy(runs=20), baselines={"a.md": stale})
    stat = result.scenarios[0]
    # Still a BLOCK (it fails every run) but not blamed on a regression, and the
    # discarded baseline is explained rather than silently dropped.
    assert stat.classification == "stable_fail"
    assert stat.baseline_rate is None
    assert any("criteria changed" in n for n in result.notes)


def test_unchanged_criteria_keep_the_baseline(tmp_path, monkeypatch):
    scn = tmp_path / "a.md"
    scn.write_text(_SCN)
    fresh = Baseline(pass_rate=0.95, criteria_hash=baseline.criteria_hash(parse(_SCN)))
    _stub_runs(monkeypatch, lambda *a, **k: _run_result(0.0))
    result = run_gate(scn, ["python", "x"], GatePolicy(runs=20), baselines={"a.md": fresh})
    assert result.scenarios[0].classification == "regression"


def test_criteria_hash_ignores_reflowing_but_not_rewriting():
    reflowed = _SCN.replace("- [D] x", "- [D]    x   ")
    assert baseline.criteria_hash(parse(_SCN)) == baseline.criteria_hash(parse(reflowed))
    assert baseline.criteria_hash(parse(_SCN)) != baseline.criteria_hash(parse(_SCN_REWRITTEN))


# --- the regression comparison itself --------------------------------------

def test_exactly_the_threshold_drop_counts():
    """0.95 - 0.75 is 0.19999999999999996 in binary floating point."""
    assert (0.95 - 0.75) < 0.20          # the trap the old comparison fell into
    assert is_regression(wilson_interval(15, 20), baseline_rate=0.95, regression_drop=0.20)


def test_regression_needs_significance_not_just_a_gap():
    # 1/3 against a 0.70 baseline is a 0.37 point-estimate drop — enough for the
    # old comparison — but three runs cannot exclude the baseline: the interval
    # still reaches 0.79, so the "drop" is indistinguishable from bad luck.
    assert not is_regression(wilson_interval(1, 3), baseline_rate=0.70, regression_drop=0.20)
    # The same pass rate with enough runs behind it is a real finding.
    assert is_regression(wilson_interval(7, 20), baseline_rate=0.70, regression_drop=0.20)


def test_run_gate_flags_regression_with_baseline(tmp_path, monkeypatch):
    scn = tmp_path / "a.md"
    scn.write_text(_SCN)
    # Now the agent fails everything; baseline says it used to pass ~95%.
    _stub_runs(monkeypatch, lambda *a, **k: _run_result(0.0))
    result = run_gate(scn, ["python", "x"], GatePolicy(runs=20), baselines={"a.md": 0.95})
    assert result.scenarios[0].classification == "regression"
    assert result.verdict == "BLOCK"


# --- the sliding baseline ---------------------------------------------------

def test_sliding_baseline_sequence_is_caught_as_a_regression(tmp_path, monkeypatch):
    """17/20 -> 14/20 -> 11/20 -> 8/20 must not walk the bar down with it.

    Every step used to be a "flaky" result that rewrote the baseline, so each
    drop was measured against the already-degraded previous run and none of them
    ever cleared the 0.20 threshold. The agent halved its pass rate and the gate
    never once said "regression".
    """
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)

    # A clean run establishes the baseline.
    r, payload = _gate_once(scn_dir, 20, 20, monkeypatch)
    assert payload["verdict"] == "SHIP" and r.exit_code == 0
    assert baseline.load(scn_dir)["a.md"].pass_rate == 1.0

    # 17/20 is not confident enough to ship and not a big enough drop to blame
    # on the build — but it must not become the new normal either.
    r, payload = _gate_once(scn_dir, 17, 20, monkeypatch)
    assert payload["scenarios"][0]["classification"] == "flaky"
    assert r.exit_code != 0
    assert baseline.load(scn_dir)["a.md"].pass_rate == 1.0

    for passes in (14, 11, 8):
        r, payload = _gate_once(scn_dir, passes, 20, monkeypatch)
        scenario = payload["scenarios"][0]
        assert scenario["classification"] == "regression", passes
        assert scenario["baseline_rate"] == 1.0, passes
        assert payload["verdict"] == "BLOCK"
        assert r.exit_code == EXIT_CODES["BLOCK"]
        # ...and the failing run never rewrites the bar it just failed.
        assert baseline.load(scn_dir)["a.md"].pass_rate == 1.0


# --- CLI wiring -------------------------------------------------------------

def test_gate_cli_writes_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)

    r, payload = _gate_once(scn_dir, 20, 20, monkeypatch)
    assert r.exit_code == 0, r.output
    assert payload["baseline_updated"] == ["a.md"]
    data = json.loads(baseline.baseline_path().read_text())
    # The section key is the target relative to the working directory.
    assert data["scenarios"]["a.md"]["pass_rate"] == 1.0
    assert data["scenarios"]["a.md"]["criteria_hash"]


def test_gate_cli_does_not_write_a_baseline_unless_it_ships(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)

    r, payload = _gate_once(scn_dir, 14, 20, monkeypatch)
    assert payload["verdict"] == "CONDITIONAL" and r.exit_code != 0
    assert payload["baseline_updated"] == []
    assert not baseline.baseline_path().exists()


def test_gate_cli_no_baseline_flag_skips_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    (scn_dir / "a.md").write_text(_SCN)

    r, _ = _gate_once(scn_dir, 20, 20, monkeypatch, extra_args=("--no-baseline",))
    assert r.exit_code == 0, r.output
    assert not baseline.baseline_path().exists()


@pytest.mark.parametrize("bad", ["not json at all", "[]", '{"scenarios": "oops"}'])
def test_corrupt_baseline_file_is_ignored_not_fatal(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    baseline.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    baseline.baseline_path().write_text(bad, encoding="utf-8")
    assert baseline.load(tmp_path / "scenarios") == {}
