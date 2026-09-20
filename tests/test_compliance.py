"""The assurance report must grade the evidence, not the headline verdict.

A SHIP with a confirmed vulnerability behind it is not approved, and a
certificate whose signature does not verify proves nothing however good its
numbers read. These tests pin both, plus the framework cross-references a
reviewer looks for.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.compliance import build_assurance, render_markdown
from checkpoint.compliance.report import APPROVED, CONDITIONAL, REJECTED


def _cert(verdict="SHIP"):
    return {
        "verdict": verdict,
        "subject": {"agent": "support-bot", "harness": "sha256:abc", "model": "gpt-4o-mini",
                    "commit_sha": "deadbeef"},
        "gate_id": "abcd1234",
        "issued_at": "2026-08-31T00:00:00+00:00",
        "expires_at": "2026-11-29T00:00:00+00:00",
        "evidence": {"scenarios": [
            {"scenario": "happy.md", "pass_rate": 1.0, "ci_low": 0.84, "ci_high": 1.0,
             "classification": "stable_pass"},
        ]},
    }


def _redteam(entries):
    return {"entries": entries, "vulnerable": any(not e["resisted"] for e in entries)}


def _clean_redteam():
    """A red-team run where nothing landed — the only evidence that earns APPROVED."""
    return _redteam([
        {"scenario": "a", "category": "ASI04", "classification": "stable_pass", "resisted": True},
    ])


def test_overall_verdicts():
    assert build_assurance(_cert("SHIP"), _clean_redteam())["overall"] == APPROVED
    assert build_assurance(_cert("BLOCK"), _clean_redteam())["overall"] == REJECTED
    assert build_assurance(_cert("CONDITIONAL"), _clean_redteam())["overall"] == CONDITIONAL
    # A confirmed vulnerability rejects even a shipping gate.
    rt = _redteam([{"scenario": "a", "category": "ASI04", "classification": "stable_fail", "resisted": False}])
    assert build_assurance(_cert("SHIP"), rt)["overall"] == REJECTED
    # A flaky (non-confirmed) vulnerability is conditional.
    rt2 = _redteam([{"scenario": "a", "category": "ASI06", "classification": "flaky", "resisted": False}])
    assert build_assurance(_cert("SHIP"), rt2)["overall"] == CONDITIONAL


def test_security_section_maps_frameworks():
    rt = _redteam([
        {"scenario": "a", "category": "ASI04", "classification": "stable_pass", "resisted": True},
        {"scenario": "b", "category": "ASI10", "classification": "stable_fail", "resisted": False},
    ])
    report = build_assurance(_cert("SHIP"), rt)
    by_cat = report["security"]["by_category"]
    assert by_cat["ASI04"]["tested"] == 1 and by_cat["ASI04"]["vulnerable"] == 0
    assert by_cat["ASI10"]["vulnerable"] == 1
    assert "eu_ai_act" in by_cat["ASI10"]["frameworks"]
    assert report["security"]["vulnerabilities"] == 1


def test_render_markdown_sections():
    md = render_markdown(build_assurance(_cert("SHIP"), _clean_redteam()))
    assert "# Agent Assurance Report" in md
    assert "APPROVED" in md
    assert "support-bot" in md
    assert "OWASP Agentic Top 10" in md
    assert "Art. 12" in md  # audit-trail obligation


def _signed_certificate(tmp_path, monkeypatch):
    """A real signed certificate, so the signature genuinely verifies."""
    from checkpoint.gate import certificate as cert_mod
    from checkpoint.gate.verdict import (
        GatePolicy,
        GateResult,
        decide_verdict,
        summarize_scenario,
    )

    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))  # isolate the signing key
    policy = GatePolicy(runs=20)
    stat = summarize_scenario("happy.md", [100.0] * 20, [True] * 20, policy)
    verdict, code = decide_verdict([stat], policy)
    result = GateResult(verdict=verdict, scenarios=[stat], policy=policy, exit_code=code)
    signed = cert_mod.LocalSigner().sign(cert_mod.build_certificate(
        result, agent="bot", command=["python", "a.py"], model="gpt-4o-mini"))
    path = tmp_path / "cert.json"
    path.write_text(json.dumps(signed))
    return path


def test_report_cli_end_to_end(tmp_path, monkeypatch):
    cert_file = _signed_certificate(tmp_path, monkeypatch)
    rt_file = tmp_path / "rt.json"
    rt_file.write_text(json.dumps(_redteam([
        {"scenario": "atk", "category": "ASI04", "classification": "stable_pass", "resisted": True}])))
    out = tmp_path / "report.md"

    result = CliRunner().invoke(main, [
        "report", "--certificate", str(cert_file), "--redteam", str(rt_file),
        "--out", str(out), "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall"] == APPROVED
    assert payload["gate"]["signature_valid"] is True
    assert out.is_file() and "Agent Assurance Report" in out.read_text()


def test_report_rejects_a_certificate_that_was_edited_after_signing(tmp_path, monkeypatch):
    """Good numbers in an unverifiable certificate may not be the signed ones."""
    cert_file = _signed_certificate(tmp_path, monkeypatch)
    tampered = json.loads(cert_file.read_text())
    tampered["subject"]["agent"] = "some-other-bot"
    cert_file.write_text(json.dumps(tampered))

    result = CliRunner().invoke(main, ["report", "--certificate", str(cert_file), "--json"])
    payload = json.loads(result.output)
    assert payload["gate"]["signature_valid"] is False
    assert payload["overall"] == REJECTED
    assert result.exit_code == 1
