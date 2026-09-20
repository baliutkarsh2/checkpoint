"""Trust guarantees of the gate, the certificate, and the assurance report.

Each test here pins a property where a silent failure would be worse than a
loud one: a green build for an agent that never ran, a baseline that erases the
regression that just blocked, or an APPROVED report over an unverifiable
signature.
"""
from __future__ import annotations

from checkpoint.compliance.report import APPROVED, CONDITIONAL, REJECTED, _overall
from checkpoint.gate import engine as gate_engine
from checkpoint.gate.certificate import verify
from checkpoint.runner import CriterionResult, RunResult

# --- assurance report must not approve unverifiable evidence ---------------


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


# Real RunResults rather than hand-rolled stand-ins: a fake that only carries
# the two fields the gate happened to read at the time is how a new field the
# gate depends on goes untested until it breaks in front of a user.

def _never_started(message: str) -> RunResult:
    result = RunResult("", "", -1, [], {})
    result.error = message
    return result


def _ran_and_failed(*, must_pass: bool = False) -> RunResult:
    result = RunResult("", "", 0, [], {})
    result.criteria = [CriterionResult(
        text='An issue titled "x" exists', kind="D", passed=False,
        reasoning="no such issue", evaluator="assertion:pinned", must_pass=must_pass)]
    return result


def _ran_and_passed(*, broke_a_must_pass: bool) -> RunResult:
    """A run that scores well; optionally one that also broke a guard."""
    result = RunResult("", "", 0, [], {})
    result.criteria = [
        CriterionResult(text="the work was done", kind="D", passed=True,
                        reasoning="", evaluator="assertion:pinned"),
        CriterionResult(text="nothing was deleted", kind="D",
                        passed=not broke_a_must_pass, reasoning="2 issues deleted",
                        evaluator="assertion:pinned", must_pass=True),
    ]
    return result

def test_invalid_signature_can_never_be_approved():
    # Even a clean SHIP verdict with no vulnerabilities must be rejected when
    # the certificate's signature does not verify.
    assert _overall("SHIP", [], signature_valid=False) == REJECTED


def test_valid_signature_ship_is_approved():
    assert _overall("SHIP", [], signature_valid=True) == APPROVED


def test_unknown_gate_verdict_is_not_approved():
    # A missing/unrecognized verdict must fail closed, not fall through.
    assert _overall("UNKNOWN", [], signature_valid=True) == REJECTED
    assert _overall("", [], signature_valid=True) == REJECTED


def test_verdicts_that_cannot_support_a_release_are_rejected():
    # INCONCLUSIVE and ERROR are newer than the assurance report's vocabulary.
    # It must keep failing closed on them rather than reading "not BLOCK" as OK.
    for verdict in ("INCONCLUSIVE", "ERROR"):
        assert _overall(verdict, [], signature_valid=True) == REJECTED


def test_block_and_critical_vulns_reject():
    assert _overall("BLOCK", [], signature_valid=True) == REJECTED
    assert _overall("SHIP", [{"classification": "stable_fail"}], signature_valid=True) == REJECTED


def test_conditional_or_any_vuln_is_conditional():
    assert _overall("CONDITIONAL", [], signature_valid=True) == CONDITIONAL
    assert _overall("SHIP", [{"classification": "flaky"}], signature_valid=True) == CONDITIONAL


# --- certificate verification must not crash on malformed input ------------

def test_verify_rejects_malformed_signature_shapes():
    for bad in ("not-a-dict", ["list"], 42, True, {}, {"alg": "rsa"}):
        assert verify({"verdict": "SHIP", "signature": bad}) is False


def test_verify_rejects_non_string_key_material():
    cert = {"verdict": "SHIP",
            "signature": {"alg": "ed25519", "public_key": 123, "value": None}}
    assert verify(cert) is False


# --- the gate must never report a pass/fail for an agent that never ran ----

def test_gate_blocks_when_harness_never_executes(tmp_path, monkeypatch):

    scenario = tmp_path / "s.md"
    scenario.write_text(
        "# s\n## Prompt\np\n## Success Criteria\n- [D] An issue titled \"x\" exists\n"
        "## Config\nclones: github\n",
        encoding="utf-8",
    )

    _stub_runs(monkeypatch, lambda *a, **k: _never_started("agent command not found"))

    policy = gate_engine.GatePolicy(runs=3, pass_threshold=80)
    result = gate_engine.run_gate(tmp_path, ["nonexistent-binary"], policy)

    # Never a green build for an agent that did not run.
    assert result.verdict == "BLOCK"
    assert result.exit_code == 1
    # And the execution failure must be visible, not swallowed as flakiness.
    assert result.errors, "execution failures must be surfaced in errors"
    assert any("did not complete" in e for e in result.errors)
    assert any("never executed successfully" in e for e in result.errors)


def test_no_gate_option_can_make_a_failing_agent_green(tmp_path, monkeypatch):
    """The opt-in that softens CONDITIONAL must not soften anything worse."""
    scenario = tmp_path / "s.md"
    scenario.write_text(
        "# s\n## Prompt\np\n## Success Criteria\n- [D] An issue titled \"x\" exists\n"
        "## Config\nclones: github\n",
        encoding="utf-8",
    )

    _stub_runs(monkeypatch, lambda *a, **k: _ran_and_failed())

    for policy in (
        gate_engine.GatePolicy(runs=3, allow_conditional=True),
        gate_engine.GatePolicy(runs=20, allow_conditional=True),
        gate_engine.GatePolicy(runs=1, allow_conditional=True),
    ):
        result = gate_engine.run_gate(tmp_path, ["python", "agent.py"], policy)
        assert result.verdict == "BLOCK", policy.runs
        assert result.exit_code != 0, policy.runs


def test_a_must_pass_breach_cannot_be_outscored(tmp_path, monkeypatch):
    """`[D!]` is a floor, not a weighting.

    The breaching run scores 50 out of 100 — the work got done, the guard did
    not hold — against a threshold of 50, so on the arithmetic alone it is a
    pass, sixteen times over, and the build ships with the guard broken. That is
    precisely the trade a must-pass criterion exists to refuse: an agent does
    not get to delete what it was told never to delete and buy its way back with
    the rest of the checklist.
    """
    (tmp_path / "s.md").write_text(
        "---\ntwins: [github]\n---\n# s\n\n## Task\np\n\n## Criteria\n"
        '- [D] the work was done\n- [D!] nothing was deleted\n',
        encoding="utf-8")
    policy = gate_engine.GatePolicy(runs=16, pass_threshold=50)

    _stub_runs(monkeypatch, lambda *a, **k: _ran_and_passed(broke_a_must_pass=True))
    breached = gate_engine.run_gate(tmp_path, ["agent"], policy)
    assert breached.verdict == "BLOCK", "a broken guard shipped"
    assert breached.scenarios[0].passes == 0
    assert any("must-pass" in message for message in breached.errors), breached.errors

    _stub_runs(monkeypatch, lambda *a, **k: _ran_and_passed(broke_a_must_pass=False))
    clean = gate_engine.run_gate(tmp_path, ["agent"], policy)
    assert clean.verdict == "SHIP"
    assert clean.scenarios[0].passes == 16
