"""The OWASP Agentic Top 10 (2026), plus helpers to map a scenario to a category.

Scenarios declare their category with an ``owasp:`` field in the ``## Config``
block, e.g. ``owasp: ASI04``. The red-team runner groups results by category and
reports which ones an agent failed to resist.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OwaspCategory:
    id: str
    name: str
    description: str


# OWASP Top 10 for Agentic Applications, 2026.
OWASP_AGENTIC: dict[str, OwaspCategory] = {
    c.id: c for c in [
        OwaspCategory("ASI01", "Agent Planning & Reasoning Hijack",
                      "The agent's goal or plan is subverted by crafted input."),
        OwaspCategory("ASI02", "Tool Misuse",
                      "The agent is induced to call a tool destructively or outside policy."),
        OwaspCategory("ASI03", "Identity & Privilege Abuse",
                      "The agent acts with more authority than the task warrants."),
        OwaspCategory("ASI04", "Excessive Agency",
                      "The agent takes irreversible/high-impact actions it should have refused or escalated."),
        OwaspCategory("ASI05", "Unsafe Code / Command Execution",
                      "The agent runs attacker-influenced code or commands."),
        OwaspCategory("ASI06", "Memory & Context Poisoning",
                      "Instructions hidden in tool output or state steer the agent (indirect prompt injection)."),
        OwaspCategory("ASI07", "Inter-Agent Communication Abuse",
                      "A message from another agent/party is trusted and acted on unsafely."),
        OwaspCategory("ASI08", "Cascading Failures",
                      "One failed step propagates into broader unintended state changes."),
        OwaspCategory("ASI09", "Human–Agent Trust Exploitation",
                      "Social pressure or false authority pushes the agent past a policy boundary."),
        OwaspCategory("ASI10", "Data Exfiltration & Rogue Actions",
                      "The agent leaks sensitive data or performs unrequested actions."),
    ]
}


def category_for(scenario) -> str | None:
    """Return the ASI id declared in a scenario's config, normalized, or None."""
    raw = None
    cfg = getattr(scenario, "config", None) or {}
    for key in ("owasp", "owasp_category", "asi"):
        if cfg.get(key):
            raw = str(cfg[key])
            break
    if not raw:
        return None
    token = raw.strip().upper().replace(" ", "")
    return token if token in OWASP_AGENTIC else None


def describe(category_id: str) -> OwaspCategory | None:
    return OWASP_AGENTIC.get((category_id or "").strip().upper())
