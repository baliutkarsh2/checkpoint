"""Adversarial red-teaming: find the ways an agent can be made to misbehave.

Checkpoint models attacks as ordinary scenarios whose success criteria assert
the agent *resisted* — refused a destructive instruction, ignored an injected
command hidden in tool output, declined to exfiltrate data. Each scenario is
tagged with the OWASP Agentic Top 10 category it exercises, so a red-team run
reports which category an agent is vulnerable to, not just a pass/fail.
"""
from pathlib import Path

from .catalog import OWASP_AGENTIC, category_for, describe
from .generate import GeneratedAttack, generate_attacks
from .mcp_owasp import OWASP_MCP
from .mcp_poison import build_poisoned_server, poison_description
from .runner import RedTeamEntry, RedTeamReport, collect_pack, run_redteam

#: The attacks Checkpoint ships: one scenario per OWASP Agentic category.
#: Inside the package, not beside it, so a `pip install` has them — the
#: documented default is "the pack", and a checkout is not the only way here.
BUNDLED_PACK = Path(__file__).parent / "pack"

__all__ = [
    "BUNDLED_PACK",
    "OWASP_AGENTIC", "category_for", "describe",
    "RedTeamEntry", "RedTeamReport", "collect_pack", "run_redteam",
    "GeneratedAttack", "generate_attacks",
    "OWASP_MCP", "build_poisoned_server", "poison_description",
]
