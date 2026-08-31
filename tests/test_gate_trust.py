"""Trust guarantees of the gate, the certificate, and the assurance report.

Each test here pins a property where a silent failure would be worse than a
loud one: a green build for an agent that never ran, a baseline that erases the
regression that just blocked, or an APPROVED report over an unverifiable
signature.
"""
from __future__ import annotations

from checkpoint.compliance.report import APPROVED, CONDITIONAL, REJECTED, _overall
from checkpoint.gate.certificate import verify

# --- assurance report must not approve unverifiable evidence ---------------

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
    from checkpoint.gate import engine as gate_engine

    scenario = tmp_path / "s.md"
    scenario.write_text(
        "# s\n## Prompt\np\n## Success Criteria\n- [D] An issue titled \"x\" exists\n"
        "## Config\nclones: github\n",
        encoding="utf-8",
    )

    class _Failed:
        complete = False
        error = "Harness executable not found"
        score = 0.0

    monkeypatch.setattr(gate_engine, "run_once", lambda *a, **k: _Failed())

    policy = gate_engine.GatePolicy(runs=3, pass_threshold=80)
    result = gate_engine.run_gate(tmp_path, ["nonexistent-binary"], policy)

    # Never a green build for an agent that did not run.
    assert result.verdict == "BLOCK"
    assert result.exit_code == 1
    # And the execution failure must be visible, not swallowed as flakiness.
    assert result.errors, "execution failures must be surfaced in errors"
    assert any("did not complete" in e for e in result.errors)
    assert any("never executed successfully" in e for e in result.errors)
