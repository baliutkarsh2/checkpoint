"""Signed Trust Certificates for a gate run.

A gate produces a verdict; a certificate makes that verdict a portable,
tamper-evident artifact you can attach to a release, hand to a compliance
reviewer, or show a customer's vendor-review team. It records what was tested
(the agent, its harness fingerprint, the commit, the model), the statistical
evidence per scenario, and the verdict — then signs the whole thing with
Ed25519 so any later change is detectable.

The OSS build self-signs: the public key travels inside the certificate and
`verify()` checks the signature against it. That proves integrity (the evidence
wasn't altered after issuance). Binding a certificate to an organization's trust
root — so a third party can confirm *who* issued it — is a hosted feature.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SCHEMA = "checkpoint.cert/v1"


def _canonical(obj: Any) -> bytes:
    """Deterministic JSON for signing: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def harness_fingerprint(harness_cmd: list[str]) -> str:
    return "sha256:" + hashlib.sha256(" ".join(harness_cmd).encode("utf-8")).hexdigest()


def build_certificate(
    gate_result,
    *,
    agent: str,
    harness_cmd: list[str],
    commit_sha: str | None = None,
    model: str | None = None,
    valid_days: int = 90,
) -> dict:
    """Assemble the unsigned certificate body from a GateResult."""
    now = datetime.datetime.now(datetime.timezone.utc)
    scenarios = [
        {
            "scenario": s.scenario,
            "n": s.n,
            "passes": s.passes,
            "pass_rate": round(s.pass_rate, 4),
            "ci_low": round(s.ci.low, 4),
            "ci_high": round(s.ci.high, 4),
            "pass_hat_k": {str(k): round(s.reliability(k), 4)
                           for k in (1, 2, 5, 10) if k <= s.n},
            "classification": s.classification,
            "mean_score": round(s.mean_score, 2),
        }
        for s in gate_result.scenarios
    ]
    p = gate_result.policy
    body = {
        "schema": SCHEMA,
        "subject": {
            "agent": agent,
            "harness": harness_fingerprint(harness_cmd),
            "commit_sha": commit_sha,
            "model": model,
        },
        "verdict": gate_result.verdict,
        "policy": {
            "runs": p.runs,
            "pass_threshold": p.pass_threshold,
            "confidence": p.confidence,
            "ship_min": p.ship_min,
            "block_max": p.block_max,
        },
        "evidence": {
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        },
        "issued_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(days=valid_days)).isoformat(),
    }
    # A content id derived from the canonical body — stable and reproducible.
    body["gate_id"] = hashlib.sha256(_canonical(body)).hexdigest()[:16]
    return body


class LocalSigner:
    """Ed25519 signer backed by a key file (default ~/.checkpoint/keys/ed25519)."""

    def __init__(self, key_path: Path | None = None):
        self.key_path = key_path or (_default_key_dir() / "ed25519")
        self._key = self._load_or_create()

    def _load_or_create(self) -> Ed25519PrivateKey:
        if self.key_path.exists():
            return serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        key = Ed25519PrivateKey.generate()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        try:  # best-effort tighten perms (POSIX)
            self.key_path.chmod(0o600)
        except OSError:
            pass
        return key

    def _public_b64(self) -> str:
        raw = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def sign(self, body: dict) -> dict:
        """Return the certificate with an attached signature + public key."""
        signature = self._key.sign(_canonical(body))
        return {
            **body,
            "signature": {
                "alg": "ed25519",
                "public_key": self._public_b64(),
                "value": base64.b64encode(signature).decode("ascii"),
            },
        }


def verify(certificate: dict) -> bool:
    """Verify a self-signed certificate's signature over its canonical body."""
    sig = certificate.get("signature")
    if not sig or sig.get("alg") != "ed25519":
        return False
    body = {k: v for k, v in certificate.items() if k != "signature"}
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(sig["public_key"]))
        public.verify(base64.b64decode(sig["value"]), _canonical(body))
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def is_expired(certificate: dict, *, now: datetime.datetime | None = None) -> bool:
    exp = certificate.get("expires_at")
    if not exp:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        return now > datetime.datetime.fromisoformat(exp)
    except ValueError:
        return False


def _default_key_dir() -> Path:
    import os

    override = os.environ.get("CHECKPOINT_HOME")
    base = Path(override) if override else (Path.home() / ".checkpoint")
    return base / "keys"
