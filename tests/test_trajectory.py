"""Trajectory model, metrics, and `[T]` criterion evaluation."""
from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.scenario import parse
from checkpoint.trajectory import Trajectory, compute_metrics
from checkpoint.trajectory.checker import check

REPO_ROOT = Path(__file__).resolve().parent.parent

_TRACE = [
    {"method": "GET", "path": "/repos/a/b/issues", "status": 200},
    {"method": "POST", "path": "/repos/a/b/issues", "status": 201},
    {"method": "POST", "path": "/repos/a/b/issues", "status": 201},   # redundant
    {"method": "DELETE", "path": "/repos/a/b/issues/9", "status": 404},  # error
]


def test_trajectory_from_flat_and_nested_trace():
    flat = Trajectory.from_trace(_TRACE)
    assert len(flat) == 4
    nested = Trajectory.from_trace({"github": _TRACE, "slack": []})
    assert len(nested) == 4
    assert nested.steps[0].clone == "github"


def test_metrics():
    m = compute_metrics(Trajectory.from_trace(_TRACE))
    assert m.total_calls == 4
    assert m.read_calls == 1
    assert m.write_calls == 3          # 2 POST + 1 DELETE
    assert m.error_calls == 1          # the 404
    assert m.redundant_calls == 1      # the repeated POST
    assert m.distinct_endpoints == 3
    assert m.methods == {"GET": 1, "POST": 2, "DELETE": 1}


def _check(text):
    traj = Trajectory.from_trace(_TRACE)
    return check(text, traj, compute_metrics(traj))


def test_checker_call_budgets():
    assert _check("at most 10 tool calls")[0] is True
    assert _check("no more than 2 calls")[0] is False
    assert _check("at least 3 api calls")[0] is True


def test_checker_writes_errors_redundancy_and_methods():
    assert _check("at most 2 writes")[0] is False       # 3 writes
    assert _check("no failed calls")[0] is False         # a 404 happened
    assert _check("no redundant calls")[0] is False      # one repeat
    assert _check("the agent did not call DELETE")[0] is False
    assert _check("the agent did not call PUT")[0] is True


def test_checker_unrecognized_returns_none():
    passed, _ = _check("the vibes were good")
    assert passed is None


def test_t_criterion_parsed():
    scn = parse(
        "# s\n## Prompt\np\n## Success Criteria\n- [T] at most 5 tool calls\n## Config\nclones: github\n"
    )
    kinds = [c.kind for c in scn.criteria]
    assert "T" in kinds


def test_t_criteria_end_to_end(monkeypatch):
    """A real run scores [T] criteria deterministically from the twin trace."""
    from checkpoint.runner import run_once
    from checkpoint.scenario import parse as parse_scn

    fake_harness = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"
    if not fake_harness.is_file():
        import pytest
        pytest.skip("smoke harness missing")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scn = parse_scn(
        "# trajectory\n## Setup\nseed\n## Prompt\n"
        "Create a GitHub issue in acme/webapp titled \"hello world\".\n"
        "## Success Criteria\n"
        "- [T] no failed calls\n"
        "- [T] at most 50 tool calls\n"
        "- [T] the agent did not call PUT\n"
        "## Config\nclones: github\nruns: 1\n"
    )
    result = run_once(scn, [sys.executable, str(fake_harness)])
    assert result.error is None, result.error
    traj = [c for c in result.criteria if c.kind == "T"]
    assert len(traj) == 3, [c.text for c in result.criteria]
    assert all(c.passed for c in traj), [(c.text, c.reasoning) for c in traj]
    assert all(c.evaluator == "trajectory" for c in traj)
