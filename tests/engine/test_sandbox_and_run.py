"""The sandbox lifecycle and end-to-end scenario runs on the engine."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from checkpoint.engine import Agent, RunOptions, Sandbox, SandboxError, TwinSetup, run_scenario
from checkpoint.scenario import parse
from checkpoint.twins import registry

PY = sys.executable

# A deterministic agent: creates a GitHub issue named after the task, via the
# twin URL the sandbox exports, then reports the issue number.
AGENT = textwrap.dedent('''
    import json, os, sys, urllib.request
    base = os.environ["CHECKPOINT_GITHUB_URL"]
    title = os.environ["CHECKPOINT_TASK"].split(":", 1)[-1].strip()
    req = urllib.request.Request(
        base + "/repos/acme/webapp/issues", method="POST",
        data=json.dumps({"title": title}).encode(),
        headers={"Authorization": "token " + os.environ["GITHUB_TOKEN"],
                 "Content-Type": "application/json"})
    issue = json.load(urllib.request.urlopen(req))
    mode = os.environ.get("AGENT_MODE", "")
    if mode == "crash":
        sys.exit(2)
    print(json.dumps({"text": f"Opened issue #{issue['number']}"}))
''')

SCENARIO = textwrap.dedent('''
    # File a bug

    ## Prompt
    File an issue: Login broken

    ## Success Criteria
    - [D] An issue titled "Login broken" exists

    ## Config
    twins: github
    seed: small-project
''')


@pytest.fixture
def agent_path(tmp_path: Path) -> Path:
    path = tmp_path / "agent.py"
    path.write_text(AGENT, encoding="utf-8")
    return path


# -- sandbox -------------------------------------------------------------------

def test_sandbox_lifecycle_and_env():
    with Sandbox(["github", "slack"], intercept=False) as box:
        assert box.twins == ("github", "slack")
        for name, url in box.urls.items():
            assert httpx.get(f"{url}/_health").json()["twin"] == name
        env = box.agent_env({"PATH": "x", "GITHUB_TOKEN": "a-real-token"})
        assert env["GITHUB_TOKEN"] == registry.get("github").token, "real tokens are replaced"
        assert env["CHECKPOINT_GITHUB_URL"] == box.twin_url("github")
        assert env["CHECKPOINT_TWINS"] == "github,slack"
        assert env["PATH"] == "x"
    assert not box.started


def test_prepare_resets_between_runs():
    with Sandbox(["github"], intercept=False) as box:
        box.prepare({"github": TwinSetup(seed="small-project")})
        seeded = len(box.views()["github"]["issues"]["items"])
        assert seeded > 0
        httpx.post(f"{box.twin_url('github')}/user/repos", json={"name": "extra"},
                   headers={"Authorization": "token x"})
        assert box.trace()
        box.prepare({})
        assert box.trace() == []
        assert box.views()["github"]["issues"]["items"] == []


def test_bad_seed_names_the_alternatives():
    with Sandbox(["github"], intercept=False) as box:
        with pytest.raises(SandboxError) as exc:
            box.prepare({"github": TwinSetup(seed="smal-project")})
    assert "small-project" in str(exc.value)


def test_bad_fault_config_is_reported():
    with Sandbox(["github"], intercept=False) as box:
        with pytest.raises(SandboxError) as exc:
            box.prepare({"github": TwinSetup(config={"rate_limt": 1})})
    assert "rate_limt" in str(exc.value)


def test_unknown_twin_is_rejected_up_front():
    with pytest.raises(KeyError) as exc:
        Sandbox(["jira"])
    assert "available" in str(exc.value)


def test_twinless_sandbox_starts():
    with Sandbox([], intercept=False) as box:
        assert box.urls == {}
        assert box.trace() == [] and box.views() == {}


# -- end-to-end runs --------------------------------------------------------------

def test_run_scenario_scores_a_real_agent(agent_path):
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.error is None, result.stderr
    assert result.score == 100.0
    assert result.final_answer.startswith("Opened issue #")
    assert result.twins == ["github"] and result.run_id
    created = [c for c in result.trace if c["op"] == "create"]
    assert created and created[0]["resource"] == "issues"
    key = result.views["github"]["issues"]["key"]
    seeded = {i[key] for i in result.seed_views["github"]["issues"]["items"]}
    final = {i[key] for i in result.views["github"]["issues"]["items"]}
    assert len(final - seeded) == 1


def test_sandbox_is_reused_across_runs(agent_path):
    agent = Agent(command=[PY, str(agent_path)])
    with Sandbox(["github"], intercept=False) as box:
        first = run_scenario(parse(SCENARIO), agent, sandbox=box)
        second = run_scenario(parse(SCENARIO), agent, sandbox=box)
    assert first.score == second.score == 100.0
    # Each run starts from the seed: the new issue gets the same number both times.
    assert first.final_answer == second.final_answer


def test_agent_crash_is_an_error_not_a_score(agent_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "crash")
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.error == "agent exited with code 2"
    assert result.criteria == [] and not result.complete and not result.setup_error


def test_setup_failure_is_flagged(agent_path):
    scenario = parse(SCENARIO.replace("seed: small-project", "seed: nope"))
    result = run_scenario(scenario, Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.setup_error and "sandbox setup failed" in result.error


def test_no_calls_to_the_sandbox_is_called_out(tmp_path):
    lazy = tmp_path / "lazy.py"
    lazy.write_text('print("I did it, trust me")', encoding="utf-8")
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(lazy)]),
                          options=RunOptions(intercept=False))
    assert any("made no calls" in w for w in result.warnings)
    assert result.score < 100.0


def test_read_only_fails_a_writing_agent(agent_path):
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False, read_only=True))
    assert any(c.evaluator == "read-only-guard" and not c.passed for c in result.criteria)
