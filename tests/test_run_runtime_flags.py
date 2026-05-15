"""Tests for the new `run` flags (--rate-limit, --read-only, --pass-threshold,
--no-failure-analysis, --keep-state, --fresh-seed, --seed-file, --setup-file,
-o json, -q).

The runtime-knob ones (rate-limit, read-only) hit the GitHub twin via the
runner's normal `run_once` path; we use a no-op harness that exits 0 with
{"text": "..."} so the test is deterministic and fast.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main


NOOP_HARNESS = (
    "import sys, json; "
    "print(json.dumps({'text': 'noop'}))"
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    monkeypatch.setenv("CHECKPOINT_RUNTIME_RATE_LIMIT", "")
    monkeypatch.setenv("CHECKPOINT_RUNTIME_READ_ONLY", "")
    return tmp_path


@pytest.fixture
def scenario_in_tmp(tmp_path, monkeypatch):
    """Scenario that targets the GitHub twin and uses a no-op python harness."""
    scn = tmp_path / "noop.md"
    scn.write_text(
        "# Noop\n## Prompt\nDo nothing.\n"
        "## Success Criteria\n- [D] Exactly 0 issue exists\n"
        "## Config\nclones: github\nruns: 1\ntimeout: 30\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return scn


def _harness_arg() -> str:
    return f"{sys.executable} -c \"{NOOP_HARNESS}\""


# ---------------------------------------------------------------------------
# Output-format flags (-o json / -q) — don't need real twin work
# ---------------------------------------------------------------------------

def test_run_json_output_contains_summary(isolated_home, scenario_in_tmp, monkeypatch):
    """`-o json` always emits the trailing summary object even with -q."""
    from checkpoint import runner as _runner
    from checkpoint.runner import RunResult, CriterionResult
    import checkpoint.cli as _cli

    def fake_run_once(scenario, harness_cmd, cwd=None, judge_model="gpt-4o-mini"):
        return RunResult(
            final_answer="ok", stderr="", exit_code=0, trace=[], state={},
            criteria=[CriterionResult(text="Exactly 0 issue exists", kind="D",
                                      passed=True, reasoning="ok",
                                      evaluator="deterministic")],
        )

    monkeypatch.setattr(_runner, "run_once", fake_run_once)
    monkeypatch.setattr(_cli, "run_once", fake_run_once)

    r = CliRunner().invoke(
        main, ["run", str(scenario_in_tmp), "--no-docker", "-o", "json", "-q",
               "--harness", _harness_arg(), "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output
    # Locate the trailing JSON object in stdout.
    idx = r.output.rfind("{\n  \"scenarios\":")
    assert idx >= 0, f"No summary JSON in:\n{r.output}"
    payload = json.loads(r.output[idx:])
    assert "scenarios" in payload
    assert payload["scenarios_run"] == 1
    assert payload["scenarios"][0]["satisfaction_avg"] == 100.0


# ---------------------------------------------------------------------------
# --pass-threshold semantics
# ---------------------------------------------------------------------------

def test_pass_threshold_below_score_exits_one(isolated_home, scenario_in_tmp):
    """Setting threshold to 200 (impossible) must exit 1."""
    r = CliRunner().invoke(
        main, ["run", str(scenario_in_tmp), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--pass-threshold", "200",
               "--no-failure-analysis"]
    )
    assert r.exit_code == 1, r.output


def test_pass_threshold_at_or_below_score_exits_zero(isolated_home, scenario_in_tmp):
    """Setting threshold to 0 always passes."""
    r = CliRunner().invoke(
        main, ["run", str(scenario_in_tmp), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--pass-threshold", "0",
               "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# --keep-state strips seed config from the scenario before the runner sees it
# ---------------------------------------------------------------------------

def test_keep_state_removes_seed_keys(isolated_home, tmp_path, monkeypatch):
    """Build a scenario with `seed: small-project`, run with --keep-state,
    and confirm the runner observes an empty seed config."""
    captured = {}
    from checkpoint import runner as _runner

    def fake_run_once(scenario, harness_cmd, cwd=None, judge_model="gpt-4o-mini"):
        captured["seed"] = scenario.config.get("seed")
        captured["seed_file"] = scenario.config.get("seed-file")
        captured["setup"] = scenario.setup
        from checkpoint.runner import RunResult, CriterionResult
        return RunResult(
            final_answer="x", stderr="", exit_code=0, trace=[], state={},
            criteria=[CriterionResult(text="ok", kind="D", passed=True,
                                      reasoning="ok", evaluator="deterministic")],
        )

    monkeypatch.setattr(_runner, "run_once", fake_run_once)
    # cli.py also imports run_once into its own namespace at import time;
    # patch there too so the call site uses our fake.
    import checkpoint.cli as _cli
    monkeypatch.setattr(_cli, "run_once", fake_run_once)

    scn = tmp_path / "seedy.md"
    scn.write_text(
        "# Seedy\n## Setup\nsome prose seeding\n## Prompt\ndo something\n"
        "## Success Criteria\n- [D] Exactly 0 issue exists\n"
        "## Config\nclones: github\nseed: small-project\nruns: 1\ntimeout: 30\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    r = CliRunner().invoke(
        main, ["run", str(scn), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--keep-state",
               "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output
    assert captured["seed"] is None
    assert captured["seed_file"] is None
    assert captured["setup"] == ""


def test_seed_file_flag_overrides_scenario(isolated_home, tmp_path, monkeypatch):
    captured = {}
    from checkpoint import runner as _runner

    def fake_run_once(scenario, harness_cmd, cwd=None, judge_model="gpt-4o-mini"):
        captured["seed_file"] = scenario.config.get("seed-file")
        from checkpoint.runner import RunResult, CriterionResult
        return RunResult(
            final_answer="x", stderr="", exit_code=0, trace=[], state={},
            criteria=[CriterionResult(text="ok", kind="D", passed=True,
                                      reasoning="ok", evaluator="deterministic")],
        )

    monkeypatch.setattr(_runner, "run_once", fake_run_once)
    # cli.py also imports run_once into its own namespace at import time;
    # patch there too so the call site uses our fake.
    import checkpoint.cli as _cli
    monkeypatch.setattr(_cli, "run_once", fake_run_once)
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Prompt\np\n## Success Criteria\n- [D] Exactly 0 issue exists\n"
        "## Config\nclones: github\nruns: 1\ntimeout: 30\n",
        encoding="utf-8",
    )
    seed = tmp_path / "my-seed.json"
    seed.write_text('{"state": {}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    r = CliRunner().invoke(
        main, ["run", str(scn), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--seed-file", str(seed),
               "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output
    assert captured["seed_file"] == str(seed)


def test_setup_file_flag_replaces_setup_prose(isolated_home, tmp_path, monkeypatch):
    captured = {}
    from checkpoint import runner as _runner

    def fake_run_once(scenario, harness_cmd, cwd=None, judge_model="gpt-4o-mini"):
        captured["setup"] = scenario.setup
        from checkpoint.runner import RunResult, CriterionResult
        return RunResult(
            final_answer="x", stderr="", exit_code=0, trace=[], state={},
            criteria=[CriterionResult(text="ok", kind="D", passed=True,
                                      reasoning="ok", evaluator="deterministic")],
        )

    monkeypatch.setattr(_runner, "run_once", fake_run_once)
    # cli.py also imports run_once into its own namespace at import time;
    # patch there too so the call site uses our fake.
    import checkpoint.cli as _cli
    monkeypatch.setattr(_cli, "run_once", fake_run_once)
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Setup\noriginal prose\n## Prompt\np\n"
        "## Success Criteria\n- [D] Exactly 0 issue exists\n"
        "## Config\nclones: github\nruns: 1\ntimeout: 30\n",
        encoding="utf-8",
    )
    setup = tmp_path / "my-setup.txt"
    setup.write_text("brand new setup prose", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    r = CliRunner().invoke(
        main, ["run", str(scn), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--setup-file", str(setup),
               "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output
    assert captured["setup"] == "brand new setup prose"


# ---------------------------------------------------------------------------
# --rate-limit / --read-only set the runtime env vars the runner reads
# ---------------------------------------------------------------------------

def test_rate_limit_flag_sets_env(isolated_home, scenario_in_tmp, monkeypatch):
    """The `--rate-limit` flag should poke `CHECKPOINT_RUNTIME_RATE_LIMIT`
    *before* run_once executes."""
    seen = {}
    from checkpoint import runner as _runner

    def fake_run_once(scenario, harness_cmd, cwd=None, judge_model="gpt-4o-mini"):
        seen["rate"] = os.environ.get("CHECKPOINT_RUNTIME_RATE_LIMIT")
        seen["ro"] = os.environ.get("CHECKPOINT_RUNTIME_READ_ONLY")
        from checkpoint.runner import RunResult, CriterionResult
        return RunResult(
            final_answer="x", stderr="", exit_code=0, trace=[], state={},
            criteria=[CriterionResult(text="ok", kind="D", passed=True,
                                      reasoning="ok", evaluator="deterministic")],
        )

    monkeypatch.setattr(_runner, "run_once", fake_run_once)
    # cli.py also imports run_once into its own namespace at import time;
    # patch there too so the call site uses our fake.
    import checkpoint.cli as _cli
    monkeypatch.setattr(_cli, "run_once", fake_run_once)
    r = CliRunner().invoke(
        main, ["run", str(scenario_in_tmp), "--no-docker", "-q",
               "--harness", _harness_arg(),
               "--rate-limit", "5", "--read-only",
               "--no-failure-analysis"]
    )
    assert r.exit_code == 0, r.output
    assert seen["rate"] == "5"
    assert seen["ro"] == "1"
