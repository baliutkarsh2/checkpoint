"""Phase 4 plan 04 — CLI --tag filter + .checkpoint.json + harness.json autoload.

Drives the CLI via `click.testing.CliRunner`. The fake harness just echoes
the env vars so we don't pay for an LLM judge.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main

ECHO_HARNESS = textwrap.dedent(
    """
    import json, os, sys
    out = {
        "base": os.environ.get("CHECKPOINT_BASE_URL"),
        "github": os.environ.get("CHECKPOINT_GITHUB_URL"),
        "slack": os.environ.get("CHECKPOINT_SLACK_URL"),
    }
    sys.stdout.write(json.dumps({"text": json.dumps(out)}))
    """
).strip()


@pytest.fixture
def echo_harness(tmp_path: Path) -> Path:
    p = tmp_path / "harness.py"
    p.write_text(ECHO_HARNESS)
    return p


def _scn(path: Path, title: str, tag: str | None = None, clones: str = "github") -> Path:
    body = (
        f"# {title}\n## Prompt\nhello\n"
        "## Success Criteria\n- [D] no new issues are created\n"
        f"## Config\nclones: {clones}\ntimeout: 30\n"
    )
    if tag:
        body += f"tags: {tag}\n"
    path.write_text(body)
    return path


def test_tag_filter_runs_only_matching_scenarios(echo_harness, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate from repo-root .checkpoint.json
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    _scn(scn_dir / "a.md", "smoke A", tag="smoke")
    _scn(scn_dir / "b.md", "regression B", tag="regression")
    _scn(scn_dir / "c.md", "another smoke", tag="smoke, slow")

    runner = CliRunner()
    result = runner.invoke(main, [
        "run", str(scn_dir), "--no-docker",
        "--harness", f"{sys.executable} {echo_harness}",
        "--tag", "smoke",
    ])
    assert result.exit_code == 0, result.output
    # Both smoke scenarios run; regression is skipped.
    assert "smoke A" in result.output
    assert "another smoke" in result.output
    assert "skip (tag mismatch)" in result.output
    assert "regression B" not in result.output or "skip" in result.output


def test_tag_filter_all_skipped_exits_zero(echo_harness, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scn_dir = tmp_path / "scenarios"
    scn_dir.mkdir()
    _scn(scn_dir / "a.md", "regression A", tag="regression")

    runner = CliRunner()
    result = runner.invoke(main, [
        "run", str(scn_dir), "--no-docker",
        "--harness", f"{sys.executable} {echo_harness}",
        "--tag", "smoke",
    ])
    assert result.exit_code == 0, result.output
    assert "No scenarios matched the filter" in result.output


def test_dot_checkpoint_json_autoload(echo_harness, tmp_path: Path, monkeypatch):
    """Without --harness, the CLI picks up `harness.path` from .checkpoint.json."""
    (tmp_path / ".checkpoint.json").write_text(json.dumps({
        "clones": ["github"],
        "harness": {"path": str(echo_harness)},
    }))
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Prompt\nhi\n"
        "## Success Criteria\n- [D] no new issues are created\n"
        "## Config\nclones: github\ntimeout: 30\n"
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["run", str(scn), "--no-docker"])
    assert result.exit_code == 0, result.output
    assert "Score:" in result.output


def test_harness_json_autoload(echo_harness, tmp_path: Path, monkeypatch):
    """--harness pointing at a directory containing harness.json works."""
    monkeypatch.chdir(tmp_path)
    hdir = echo_harness.parent
    (hdir / "harness.json").write_text(json.dumps({"path": echo_harness.name}))
    scn = hdir / "x.md"
    scn.write_text(
        "# x\n## Prompt\nhi\n"
        "## Success Criteria\n- [D] no new issues are created\n"
        "## Config\nclones: github\ntimeout: 30\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["run", str(scn), "--no-docker", "--harness", str(hdir)])
    assert result.exit_code == 0, result.output


def test_evaluator_source_recorded_in_trace_out(echo_harness, tmp_path: Path, monkeypatch):
    """`evaluator_model_source` shows up in --trace-out JSON (SCN-09)."""
    monkeypatch.chdir(tmp_path)
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Prompt\nhi\n## Config\nclones: github\ntimeout: 30\n"
        "evaluator-model: gpt-test-scenario\n"
    )
    trace_path = tmp_path / "trace.json"
    runner = CliRunner()
    result = runner.invoke(main, [
        "run", str(scn), "--no-docker",
        "--harness", f"{sys.executable} {echo_harness}",
        "--trace-out", str(trace_path),
    ])
    assert result.exit_code in (0, 1), result.output  # may fail criteria but should run
    dump = json.loads(trace_path.read_text())
    assert dump[0]["evaluator_model"] == "gpt-test-scenario"
    assert dump[0]["evaluator_model_source"] == "scenario"


def test_evaluator_flag_overrides_scenario(echo_harness, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Prompt\nhi\n## Config\nclones: github\ntimeout: 30\n"
        "evaluator-model: gpt-test-scenario\n"
    )
    trace_path = tmp_path / "trace.json"
    runner = CliRunner()
    result = runner.invoke(main, [
        "run", str(scn), "--no-docker",
        "--harness", f"{sys.executable} {echo_harness}",
        "--model", "gpt-test-flag",
        "--trace-out", str(trace_path),
    ])
    assert result.exit_code in (0, 1)
    dump = json.loads(trace_path.read_text())
    assert dump[0]["evaluator_model"] == "gpt-test-flag"
    assert dump[0]["evaluator_model_source"] == "flag"


def test_reuse_session_flag_is_noop(echo_harness, tmp_path: Path, monkeypatch):
    """--reuse-session prints a notice and otherwise behaves normally."""
    monkeypatch.chdir(tmp_path)
    scn = tmp_path / "x.md"
    scn.write_text(
        "# x\n## Prompt\nhi\n"
        "## Success Criteria\n- [D] no new issues are created\n"
        "## Config\nclones: github\ntimeout: 30\n"
    )
    runner = CliRunner()
    result = runner.invoke(main, [
        "run", str(scn), "--no-docker",
        "--harness", f"{sys.executable} {echo_harness}",
        "--reuse-session",
    ])
    assert "hosted sessions unavailable" in result.output
