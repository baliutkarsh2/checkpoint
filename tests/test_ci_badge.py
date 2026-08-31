"""Tests for `checkpoint badge` and `checkpoint ci init` CLI commands."""
from __future__ import annotations

import json

from click.testing import CliRunner

from checkpoint.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_run(score: float) -> dict:
    return {
        "run_id": "abc123",
        "scenario": "github-happy",
        "satisfaction": score,
        "criteria": [],
        "env": {"timestamp": "2026-05-14T00:00:00Z"},
    }


# ---------------------------------------------------------------------------
# badge command
# ---------------------------------------------------------------------------

def test_badge_url_green(tmp_path, monkeypatch):
    from checkpoint import run_record as _rr
    monkeypatch.setattr(_rr, "RUNS_DIR", tmp_path)
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    (tmp_path / "abc123.json").write_text(json.dumps(_synthetic_run(100)))
    last_run = tmp_path / "last-run.json"
    last_run.write_text(json.dumps(_synthetic_run(100)))

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "abc123"])
    assert result.exit_code == 0, result.output
    assert "brightgreen" in result.output
    assert "shields.io" in result.output


def test_badge_url_red(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    (tmp_path / "abc000.json").write_text(json.dumps(_synthetic_run(0)))

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "abc000"])
    assert result.exit_code == 0
    assert "red" in result.output


def test_badge_url_yellow(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    (tmp_path / "abc075.json").write_text(json.dumps(_synthetic_run(75)))

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "abc075"])
    assert result.exit_code == 0
    assert "yellow" in result.output


def test_badge_md_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    (tmp_path / "abc100.json").write_text(json.dumps(_synthetic_run(100)))

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "abc100", "--md"])
    assert result.exit_code == 0
    assert result.output.startswith("![checkpoint]")


def test_badge_no_record_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "nonexistent-id"])
    assert result.exit_code == 1


def test_badge_custom_label(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)
    (tmp_path / "x.json").write_text(json.dumps(_synthetic_run(100)))

    runner = CliRunner()
    result = runner.invoke(main, ["badge", "x", "--label", "myproject", "--md"])
    assert result.exit_code == 0
    assert "myproject" in result.output


# ---------------------------------------------------------------------------
# ci init command
# ---------------------------------------------------------------------------

def test_ci_init_creates_workflow(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["ci", "init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    wf = tmp_path / ".github" / "workflows" / "checkpoint.yml"
    assert wf.exists()
    content = wf.read_text()
    assert "checkpoint" in content
    assert "OPENAI_API_KEY" in content


def test_ci_init_skips_existing(tmp_path):
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    existing = wf_dir / "checkpoint.yml"
    existing.write_text("# existing")

    runner = CliRunner()
    result = runner.invoke(main, ["ci", "init", str(tmp_path)])
    assert result.exit_code == 0
    assert existing.read_text() == "# existing"  # not overwritten
    assert "already exists" in result.output.lower()


def test_ci_init_pre_commit_flag(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["ci", "init", str(tmp_path), "--pre-commit"])
    assert result.exit_code == 0, result.output
    pc = tmp_path / ".pre-commit-config.yaml"
    assert pc.exists()
    assert "checkpoint-validate" in pc.read_text()


def test_ci_init_no_pre_commit_by_default(tmp_path):
    runner = CliRunner()
    runner.invoke(main, ["ci", "init", str(tmp_path)])
    pc = tmp_path / ".pre-commit-config.yaml"
    assert not pc.exists()


# ---------------------------------------------------------------------------
# runs list --scenario filter
# ---------------------------------------------------------------------------

def test_runs_list_scenario_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    (tmp_path / "r1.json").write_text(json.dumps({**_synthetic_run(100), "scenario": "github-happy", "run_id": "r1"}))
    (tmp_path / "r2.json").write_text(json.dumps({**_synthetic_run(75), "scenario": "stripe-refund", "run_id": "r2"}))

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--scenario", "github", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["scenario"] == "github-happy"


def test_runs_list_no_filter_returns_all(tmp_path, monkeypatch):
    monkeypatch.setattr("checkpoint.cli.RUNS_DIR", tmp_path)

    (tmp_path / "r1.json").write_text(json.dumps({**_synthetic_run(100), "scenario": "github-happy", "run_id": "r1"}))
    (tmp_path / "r2.json").write_text(json.dumps({**_synthetic_run(75), "scenario": "stripe-refund", "run_id": "r2"}))

    runner = CliRunner()
    result = runner.invoke(main, ["runs", "list", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 2
