"""Signed Trust Certificates: build, sign, verify, tamper-detect."""
from __future__ import annotations

import datetime

import pytest

from checkpoint.gate import certificate as cert
from checkpoint.gate.verdict import GatePolicy, GateResult, summarize_scenario, decide_verdict


def _gate_result(scores_per_scenario: dict[str, list[float]], policy: GatePolicy) -> GateResult:
    stats = [
        summarize_scenario(name, scores, [True] * len(scores), policy)
        for name, scores in scores_per_scenario.items()
    ]
    verdict, code = decide_verdict(stats, policy)
    return GateResult(verdict=verdict, scenarios=stats, policy=policy, exit_code=code)


@pytest.fixture
def signer(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    return cert.LocalSigner()


def test_build_and_sign_roundtrip(signer):
    p = GatePolicy(runs=20)
    gr = _gate_result({"a.md": [100.0] * 20}, p)
    body = cert.build_certificate(gr, agent="bot", harness_cmd=["python", "a.py"], model="gpt-4o-mini")
    assert body["verdict"] == "SHIP"
    assert body["schema"] == cert.SCHEMA
    assert body["subject"]["harness"].startswith("sha256:")
    assert len(body["gate_id"]) == 16

    signed = signer.sign(body)
    assert cert.verify(signed) is True


def test_tampering_any_field_breaks_verification(signer):
    p = GatePolicy(runs=20)
    gr = _gate_result({"a.md": [100.0] * 20}, p)
    signed = signer.sign(cert.build_certificate(gr, agent="bot", harness_cmd=["python", "a.py"]))

    for mutate in (
        lambda c: c.__setitem__("verdict", "SHIP" if c["verdict"] != "SHIP" else "BLOCK"),
        lambda c: c["evidence"]["scenarios"][0].__setitem__("passes", 0),
        lambda c: c["subject"].__setitem__("agent", "someone-else"),
    ):
        import copy
        c = copy.deepcopy(signed)
        mutate(c)
        assert cert.verify(c) is False


def test_verify_rejects_unsigned_or_wrong_alg():
    assert cert.verify({"verdict": "SHIP"}) is False
    assert cert.verify({"verdict": "SHIP", "signature": {"alg": "rsa"}}) is False


def test_key_is_persisted_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    s1 = cert.LocalSigner()
    pub1 = s1._public_b64()
    s2 = cert.LocalSigner()  # second signer loads the same key file
    assert s2._public_b64() == pub1


def test_block_verdict_certificate(signer):
    p = GatePolicy(runs=20)
    gr = _gate_result({"a.md": [0.0] * 20}, p)  # all fail
    signed = signer.sign(cert.build_certificate(gr, agent="bot", harness_cmd=["python", "a.py"]))
    assert signed["verdict"] == "BLOCK"
    assert cert.verify(signed) is True  # a BLOCK certificate is still validly signed


def test_expiry_check(signer):
    p = GatePolicy(runs=5)
    gr = _gate_result({"a.md": [100.0] * 5}, p)
    body = cert.build_certificate(gr, agent="bot", harness_cmd=["python", "a.py"], valid_days=1)
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
    assert cert.is_expired(body, now=future) is True
    assert cert.is_expired(body) is False
