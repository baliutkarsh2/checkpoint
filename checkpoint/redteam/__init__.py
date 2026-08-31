"""Adversarial red-teaming: find the ways an agent can be made to misbehave.

Checkpoint models attacks as ordinary scenarios whose success criteria assert
the agent *resisted* — refused a destructive instruction, ignored an injected
command hidden in tool output, declined to exfiltrate data. Each scenario is
tagged with the OWASP Agentic Top 10 category it exercises, so a red-team run
reports which category an agent is vulnerable to, not just a pass/fail.
"""
from .catalog import OWASP_AGENTIC, category_for, describe
from .runner import RedTeamEntry, RedTeamReport, collect_pack, run_redteam
from .generate import GeneratedAttack, generate_attacks

__all__ = [
    "OWASP_AGENTIC", "category_for", "describe",
    "RedTeamEntry", "RedTeamReport", "collect_pack", "run_redteam",
    "GeneratedAttack", "generate_attacks",
]
