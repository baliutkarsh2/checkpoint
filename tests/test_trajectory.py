"""The trajectory model and metrics, and `[T]` criteria end to end."""
from __future__ import annotations

import sys
from pathlib import Path

from checkpoint.scenario import parse
from checkpoint.trajectory import Trajectory, compute_metrics

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
    assert nested.steps[0].twin == "github"


def test_metrics():
    m = compute_metrics(Trajectory.from_trace(_TRACE))
    assert m.total_calls == 4
    assert m.read_calls == 1
    assert m.write_calls == 3          # 2 POST + 1 DELETE
    assert m.error_calls == 1          # the 404
    assert m.redundant_calls == 1      # the repeated POST
    assert m.distinct_endpoints == 3
    assert m.methods == {"GET": 1, "POST": 2, "DELETE": 1}


def test_t_criterion_parsed():
    scn = parse(
        "# s\n## Prompt\np\n## Success Criteria\n- [T] at most 5 tool calls\n## Config\nclones: github\n"
    )
    kinds = [c.kind for c in scn.criteria]
    assert "T" in kinds


def test_t_criteria_end_to_end(monkeypatch):
    """A real run scores [T] criteria deterministically from the twin trace."""
    from checkpoint.engine import Agent, run_scenario
    from checkpoint.scenario import parse as parse_scn

    # The packaged demo agent, not an example: it ships in the wheel, so this
    # cannot start skipping because a directory was reorganised.
    fake_harness = REPO_ROOT / "checkpoint" / "demo" / "harness_fake.py"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scn = parse_scn(
        "# trajectory\n## Setup\nseed\n## Prompt\n"
        "Create a GitHub issue in acme/webapp titled \"hello world\".\n"
        "## Success Criteria\n"
        "- [T] no failed calls\n"
        "- [T] at most 50 tool calls\n"
        "- [T] the agent did not call PUT\n"
        # Seeded, so acme/webapp exists: against an empty twin the agent's issue
        # call 404s and "no failed calls" fails for a reason that has nothing to
        # do with what this test is checking.
        "## Config\nclones: github\nseed: small-project\nruns: 1\n"
    )
    result = run_scenario(scn, Agent(command=[sys.executable, str(fake_harness)]))
    assert result.error is None, result.error
    traj = [c for c in result.criteria if c.kind == "T"]
    assert len(traj) == 3, [c.text for c in result.criteria]
    assert all(c.passed for c in traj), [(c.text, c.reasoning) for c in traj]
    assert all(c.evaluator == "assertion:pattern" for c in traj)
    assert all(c.assertion for c in traj), [(c.text, c.assertion) for c in traj]
