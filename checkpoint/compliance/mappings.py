"""Reference crosswalk from OWASP Agentic Top 10 to other frameworks.

These are informational pointers to help a reviewer locate the relevant control,
not legal advice or a certification of compliance. Citations are to the public
frameworks as of 2026.
"""
from __future__ import annotations

# Each OWASP Agentic category -> the frameworks a reviewer typically cross-checks.
FRAMEWORK_MAP: dict[str, dict[str, str]] = {
    "ASI01": {"nist_ai_rmf": "MANAGE-2 (planning/goal integrity)", "eu_ai_act": "Art. 15 (robustness)"},
    "ASI02": {"nist_ai_rmf": "MEASURE-2 (tool-use safety)", "eu_ai_act": "Art. 15 (accuracy/robustness)"},
    "ASI03": {"nist_ai_rmf": "GOVERN-1 (identity & authority)", "eu_ai_act": "Art. 14 (human oversight)"},
    "ASI04": {"nist_ai_rmf": "MANAGE-4 (high-impact actions)", "eu_ai_act": "Art. 14 (human oversight)"},
    "ASI05": {"nist_ai_rmf": "MEASURE-2 (unsafe execution)", "eu_ai_act": "Art. 15 (cybersecurity)"},
    "ASI06": {"nist_ai_rmf": "MAP-5 (context/data integrity)", "eu_ai_act": "Art. 15 (robustness)"},
    "ASI07": {"nist_ai_rmf": "GOVERN-6 (third-party/inter-agent)", "eu_ai_act": "Art. 15 (robustness)"},
    "ASI08": {"nist_ai_rmf": "MANAGE-2 (failure containment)", "eu_ai_act": "Art. 15 (robustness)"},
    "ASI09": {"nist_ai_rmf": "GOVERN-5 (human-AI trust)", "eu_ai_act": "Art. 14 (human oversight)"},
    "ASI10": {"nist_ai_rmf": "MEASURE-2 (data exfiltration)", "eu_ai_act": "Art. 10 (data governance)"},
}

# The audit-trail obligation the whole report supports.
EU_AI_ACT_LOGGING = "Art. 12 — automatic record-keeping of risk-relevant events"


def frameworks_for(owasp_id: str) -> dict[str, str]:
    return FRAMEWORK_MAP.get((owasp_id or "").strip().upper(), {})
