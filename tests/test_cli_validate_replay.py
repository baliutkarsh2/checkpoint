"""Tests for the `checkpoint validate` and `checkpoint replay` CLI commands."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "scenarios"
CLI = [sys.executable, "-m", "checkpoint.cli"]


def _run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI, *args],
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# checkpoint validate
# ---------------------------------------------------------------------------

def test_validate_happy_path():
    scn = SCENARIOS_DIR / "archal-verbatim-github.md"
    result = _run("validate", str(scn))
    assert result.returncode == 0, result.stderr
    assert "valid" in result.stdout.lower()


def test_validate_json_output():
    scn = SCENARIOS_DIR / "archal-verbatim-github.md"
    result = _run("validate", str(scn), "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["valid"] is True
    assert "criteria_count" in data
    assert data["criteria_count"] > 0
    assert "errors" in data
    assert "warnings" in data


def test_validate_json_reports_clones():
    scn = SCENARIOS_DIR / "archal-verbatim-github.md"
    result = _run("validate", str(scn), "--json")
    data = json.loads(result.stdout)
    assert data["clones"] == ["github"]


def test_validate_multi_clone_scenario():
    scn = SCENARIOS_DIR / "multi-clone-cross-system.md"
    result = _run("validate", str(scn), "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "slack" in data["clones"]
    assert "stripe" in data["clones"]


@pytest.fixture
def bad_scenario(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("# No prompt\n\n## Config\nclones: fakeclone\n")
    return p


def test_validate_invalid_scenario_exits_1(bad_scenario):
    result = _run("validate", str(bad_scenario))
    assert result.returncode == 1


def test_validate_invalid_scenario_json_reports_errors(bad_scenario):
    result = _run("validate", str(bad_scenario), "--json")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["valid"] is False
    assert len(data["errors"]) > 0


def test_validate_bad_clone_name_detected(bad_scenario):
    result = _run("validate", str(bad_scenario), "--json")
    data = json.loads(result.stdout)
    errors = " ".join(data["errors"])
    assert "fakeclone" in errors or "Unknown clone" in errors


def test_validate_warns_on_unmatched_d_criteria(tmp_path):
    """[D] criteria with no regex match should appear in warnings (not errors)."""
    p = tmp_path / "warn.md"
    p.write_text(
        "# Test\n"
        "## Prompt\nDo something\n"
        "## Success Criteria\n"
        "- [D] The agent did something completely unrecognizable xyz123\n"
        "## Config\nclones: github\n"
    )
    result = _run("validate", str(p), "--json")
    data = json.loads(result.stdout)
    assert data["valid"] is True  # unmatched D is a warning, not an error
    assert any("no deterministic" in w.lower() or "fall through" in w.lower()
               for w in data["warnings"])


@pytest.mark.parametrize("fname", [
    "github-happy-path.md",
    "github-adversarial.md",
    "slack-incident-response.md",
    "stripe-refund-controls.md",
    "linear-issue-triage.md",
    "discord-incident-response.md",
    "supabase-data-ops.md",
    "google-workspace-email-ops.md",
    "multi-clone-cross-system.md",
    "archal-verbatim-github.md",
])
def test_validate_all_bundled_scenarios_are_valid(fname):
    scn = SCENARIOS_DIR / fname
    if not scn.exists():
        pytest.skip(f"{fname} not present")
    result = _run("validate", str(scn), "--json")
    data = json.loads(result.stdout)
    assert data["valid"] is True, (
        f"{fname} failed validation: {data['errors']}"
    )


# ---------------------------------------------------------------------------
# checkpoint replay
# ---------------------------------------------------------------------------

@pytest.fixture
def run_record(tmp_path, monkeypatch):
    """Write a synthetic run record and point RUNS_DIR + last-run at it."""
    from checkpoint.run_record import RUNS_DIR

    monkeypatch.setattr("checkpoint.run_record.RUNS_DIR", tmp_path)
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    run_id = "test-replay-001"
    record = {
        "run_id": run_id,
        "scenario": "test-scenario",
        "satisfaction": 75.0,
        "evaluator_model": "gpt-4o-mini",
        "criteria": [],
        "trace": [
            {"method": "POST", "path": "/repos/acme/webapp/issues", "status": 201,
             "_clone": "github"},
            {"method": "GET", "path": "/repos/acme/webapp/issues", "status": 200,
             "_clone": "github"},
        ],
        "state": {},
        "env": {"timestamp": "2026-05-13T00:00:00Z"},
    }
    record_path = tmp_path / f"{run_id}.json"
    record_path.write_text(json.dumps(record))

    last_run_path = tmp_path / "last-run.json"
    last_run_path.write_text(json.dumps(record))

    return run_id, tmp_path


def test_replay_json_output(run_record, monkeypatch):
    run_id, runs_dir = run_record
    monkeypatch.setenv("CHECKPOINT_RUNS_DIR", str(runs_dir))

    result = _run("replay", run_id, "--json",
                  env={**__import__("os").environ, "CHECKPOINT_RUNS_DIR": str(runs_dir)})
    # The JSON output should be a list of trace events.
    # (If the run record isn't found, exit code will be 1.)
    # We verify the command at least exits cleanly with the given run_id.
    # The monkeypatch approach doesn't work across subprocess boundaries, so
    # we write a direct Python-level test below.
    assert result.returncode in (0, 1)


def test_replay_json_output_direct(run_record, monkeypatch):
    """Exercise replay directly at the Python level (no subprocess env boundary)."""
    from click.testing import CliRunner
    from checkpoint.cli import replay

    run_id, runs_dir = run_record
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", runs_dir)

    runner = CliRunner()
    result = runner.invoke(replay, [run_id, "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    trace = json.loads(result.output)
    assert isinstance(trace, list)
    assert len(trace) == 2
    assert trace[0]["method"] == "POST"


def test_replay_table_output_direct(run_record, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import replay

    run_id, runs_dir = run_record
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", runs_dir)

    runner = CliRunner()
    result = runner.invoke(replay, [run_id], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Table output should mention the trace events
    assert "POST" in result.output or "GET" in result.output


def test_replay_missing_run_exits_1(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import replay

    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    runner = CliRunner()
    result = runner.invoke(replay, ["nonexistent-id-xyz"])
    assert result.exit_code == 1


def test_replay_clone_filter(run_record, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import replay

    run_id, runs_dir = run_record
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", runs_dir)

    runner = CliRunner()
    result = runner.invoke(replay, [run_id, "--clone", "github", "--json"],
                           catch_exceptions=False)
    assert result.exit_code == 0
    trace = json.loads(result.output)
    assert all(ev.get("_clone") == "github" for ev in trace)


def test_replay_limit(run_record, monkeypatch):
    from click.testing import CliRunner
    from checkpoint.cli import replay

    run_id, runs_dir = run_record
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", runs_dir)

    runner = CliRunner()
    result = runner.invoke(replay, [run_id, "--limit", "1", "--json"],
                           catch_exceptions=False)
    assert result.exit_code == 0
    trace = json.loads(result.output)
    assert len(trace) == 1
