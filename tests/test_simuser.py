"""Multi-turn simulated users: persona, calibration, and the conversation loop."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from checkpoint.scenario import parse
from checkpoint.simuser import LLMSimulatedUser, Persona, ScriptedUser, UserTurn, simulate
from checkpoint.simuser.calibration import compute_calibration

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"

_SCENARIO = (
    "# multi-turn\n## Setup\nfresh\n## Prompt\n"
    "Create a GitHub issue in acme/webapp titled \"hello world\".\n"
    "## Success Criteria\n- [D] An issue titled \"hello world\" exists\n"
    "## Config\nclones: github\n"
)


def _scn():
    return parse(_SCENARIO)


# --- calibration ------------------------------------------------------------

def test_calibration_shapes():
    p = Persona("u", "goal")
    assert compute_calibration(1, 6, p, satisfied=True, gave_up=False) == 0.7
    assert compute_calibration(3, 6, p, satisfied=True, gave_up=False) == 0.9
    assert compute_calibration(1, 6, p, satisfied=False, gave_up=True) == 0.5
    assert compute_calibration(6, 6, p, satisfied=False, gave_up=False) == 0.5
    adv = Persona("u", "goal", adversarial=True)
    assert compute_calibration(3, 6, adv, satisfied=True, gave_up=False) == 0.81


def test_scripted_user_replays_then_gives_up():
    u = ScriptedUser([UserTurn(message="more"), UserTurn(satisfied=True)])
    assert u.next([], Persona("u", "g")).message == "more"
    assert u.next([], Persona("u", "g")).satisfied is True
    assert u.next([], Persona("u", "g")).gave_up is True  # exhausted


def test_llm_user_parses_client_json():
    def factory():
        payload = json.dumps({"message": "please retry", "satisfied": False, "gave_up": False})
        choice = SimpleNamespace(message=SimpleNamespace(content=payload))
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[choice]))))
        return client

    u = LLMSimulatedUser("gpt-4o-mini", client_factory=factory)
    turn = u.next([{"role": "assistant", "content": "hi"}], Persona("u", "g"))
    assert turn.message == "please retry" and not turn.satisfied


# --- end-to-end conversation (offline: scripted user + fake harness) --------

@pytest.mark.skipif(not FAKE_HARNESS.is_file(), reason="fake harness missing")
def test_simulate_satisfied_in_one_turn(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    persona = Persona(name="alice", goal='Create a GitHub issue in acme/webapp titled "hello world".', patience=3)
    user = ScriptedUser([UserTurn(satisfied=True, message="thanks!")])
    res = simulate(_scn(), [sys.executable, str(FAKE_HARNESS)], persona,
                   user=user, max_turns=4)
    assert res.error is None, res.error
    assert res.satisfied is True
    assert res.turns == 1
    assert res.result is not None and res.result.score == 100.0  # [D] met via accumulated state
    assert res.calibration == 0.7
    # transcript: user goal, assistant answer, closing user message
    assert res.transcript[0]["role"] == "user"
    assert any(t["role"] == "assistant" for t in res.transcript)


@pytest.mark.skipif(not FAKE_HARNESS.is_file(), reason="fake harness missing")
def test_simulate_runs_multiple_turns(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    persona = Persona(name="bob", goal='Create a GitHub issue in acme/webapp titled "hello world".', patience=5)
    user = ScriptedUser([UserTurn(message="and confirm it exists"), UserTurn(satisfied=True)])
    res = simulate(_scn(), [sys.executable, str(FAKE_HARNESS)], persona,
                   user=user, max_turns=5)
    assert res.error is None, res.error
    assert res.turns == 2
    assert res.satisfied is True


def test_simulate_unknown_clone_errors():
    scn = parse("# x\n## Prompt\np\n## Success Criteria\n- [D] x\n## Config\nclones: notaclone\n")
    res = simulate(scn, ["python", "x"], Persona("u", "g"))
    assert res.error and "Unknown clones" in res.error


def test_simulate_cli_json(tmp_path, monkeypatch):
    """The CLI renders a stubbed simulation as JSON with the calibration."""
    from click.testing import CliRunner

    import checkpoint.simuser as simmod
    from checkpoint.cli import main
    from checkpoint.runner import CriterionResult, RunResult
    from checkpoint.simuser.engine import SimResult

    rr = RunResult("done", "", 0, [], {})
    rr.criteria = [CriterionResult(text="An issue exists", kind="D", passed=True,
                                   reasoning="found", evaluator="deterministic")]
    fake = SimResult(
        persona_name="alice", turns=2,
        transcript=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}],
        satisfied=True, gave_up=False, result=rr, calibration=0.9,
    )
    monkeypatch.setattr(simmod, "simulate", lambda *a, **k: fake)

    scn = tmp_path / "s.md"
    scn.write_text(_SCENARIO)
    result = CliRunner().invoke(main, [
        "simulate", str(scn), "--harness", "python agent.py", "-o", "json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["satisfied"] is True
    assert payload["turns"] == 2
    assert payload["calibration"] == 0.9
    assert payload["criteria"][0]["passed"] is True
