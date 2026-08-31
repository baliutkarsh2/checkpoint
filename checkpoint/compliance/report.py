"""Assemble and render an Agent Assurance Report."""
from __future__ import annotations

import datetime

from ..redteam.catalog import describe
from .mappings import EU_AI_ACT_LOGGING, frameworks_for

# Graduated verdict, following the pre-deployment assurance / Trust Certificate
# framing (Approved / Conditional / Rejected).
APPROVED, CONDITIONAL, REJECTED = "APPROVED", "CONDITIONAL", "REJECTED"


def _overall(gate_verdict: str, vulns: list[dict]) -> str:
    critical = [v for v in vulns if v.get("classification") == "stable_fail"]
    if gate_verdict == "BLOCK" or critical:
        return REJECTED
    if gate_verdict == "CONDITIONAL" or vulns:
        return CONDITIONAL
    return APPROVED


def build_assurance(certificate: dict, redteam: dict | None = None,
                    *, signature_valid: bool | None = None) -> dict:
    """Combine a signed gate certificate and a red-team report into a report dict."""
    redteam = redteam or {}
    entries = redteam.get("entries", []) or []
    vulns = [e for e in entries if not e.get("resisted", True)]

    gate_verdict = certificate.get("verdict", "UNKNOWN")
    overall = _overall(gate_verdict, vulns)

    # Category coverage (from the red-team entries) with framework references.
    categories: dict[str, dict] = {}
    for e in entries:
        cat = (e.get("category") or "").upper()
        if not cat:
            continue
        info = categories.setdefault(cat, {"tested": 0, "vulnerable": 0})
        info["tested"] += 1
        if not e.get("resisted", True):
            info["vulnerable"] += 1

    return {
        "schema": "checkpoint.assurance/v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "overall": overall,
        "subject": certificate.get("subject", {}),
        "gate": {
            "verdict": gate_verdict,
            "gate_id": certificate.get("gate_id"),
            "issued_at": certificate.get("issued_at"),
            "expires_at": certificate.get("expires_at"),
            "signature_valid": signature_valid,
            "scenarios": certificate.get("evidence", {}).get("scenarios", []),
        },
        "security": {
            "attacks_run": len(entries),
            "vulnerabilities": len(vulns),
            "by_category": {
                cat: {
                    **info,
                    "name": (describe(cat).name if describe(cat) else cat),
                    "frameworks": frameworks_for(cat),
                }
                for cat, info in sorted(categories.items())
            },
        },
        "audit_trail": EU_AI_ACT_LOGGING,
    }


def render_markdown(report: dict) -> str:
    subj = report.get("subject", {})
    gate = report.get("gate", {})
    sec = report.get("security", {})
    lines = [
        "# Agent Assurance Report",
        "",
        f"**Verdict: {report['overall']}**  ",
        f"Generated: {report['generated_at']}",
        "",
        "## Subject",
        f"- Agent: `{subj.get('agent', '?')}`",
        f"- Harness: `{subj.get('harness', '?')}`",
        f"- Commit: `{subj.get('commit_sha') or 'n/a'}`",
        f"- Model: `{subj.get('model') or 'n/a'}`",
        "",
        "## Gate",
        f"- Verdict: **{gate.get('verdict', '?')}**  (gate id `{gate.get('gate_id', '?')}`)",
        f"- Certificate signature: "
        + ("valid" if gate.get("signature_valid") else
           ("INVALID" if gate.get("signature_valid") is False else "not checked")),
        f"- Issued: {gate.get('issued_at', '?')} · Expires: {gate.get('expires_at', '?')}",
        "",
        "| Scenario | Pass rate | 95% CI | Classification |",
        "|---|---|---|---|",
    ]
    for s in gate.get("scenarios", []):
        lines.append(
            f"| {s.get('scenario', '?')} | {s.get('pass_rate', 0) * 100:.0f}% | "
            f"[{s.get('ci_low', 0) * 100:.0f}%, {s.get('ci_high', 0) * 100:.0f}%] | "
            f"{s.get('classification', '?')} |"
        )
    lines += [
        "",
        "## Security (OWASP Agentic Top 10)",
        f"- Attacks run: {sec.get('attacks_run', 0)} · Vulnerabilities: **{sec.get('vulnerabilities', 0)}**",
        "",
        "| OWASP | Category | Tested | Vulnerable | NIST AI RMF | EU AI Act |",
        "|---|---|---|---|---|---|",
    ]
    for cat, info in sec.get("by_category", {}).items():
        fw = info.get("frameworks", {})
        lines.append(
            f"| {cat} | {info.get('name', cat)} | {info.get('tested', 0)} | "
            f"{info.get('vulnerable', 0)} | {fw.get('nist_ai_rmf', '-')} | {fw.get('eu_ai_act', '-')} |"
        )
    lines += [
        "",
        "## Audit trail",
        f"- {report.get('audit_trail', '')}",
        "",
        "---",
        "_Informational cross-references, not a certification of legal compliance._",
        "",
    ]
    return "\n".join(lines)
