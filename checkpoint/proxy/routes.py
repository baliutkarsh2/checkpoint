"""Domain -> twin URL + bootstrap token registry.

Hardcoded for Phase 1: only api.github.com. Adding slack.com / api.stripe.com
in Phase 3 is a one-file change to the seeded dict below.

Tokens match SCOPE §3 / REQUIREMENTS.md GH-02 / SL-02 / ST-03 exactly so an
Archal-authored harness using the real bootstrap-token sees no diff.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional

# Per SCOPE §3.2 / REQUIREMENTS.md GH-02.
GITHUB_BOOTSTRAP_TOKEN = "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"


@dataclass(frozen=True)
class Route:
    domain: str
    twin_url: str  # filled in by the runner before the sidecar starts
    bootstrap_token: str


# Seeded with placeholder twin URLs — runner overwrites via register().
_ROUTES: Dict[str, Route] = {
    "api.github.com": Route(
        domain="api.github.com",
        twin_url="",
        bootstrap_token=GITHUB_BOOTSTRAP_TOKEN,
    ),
}


def register(domain: str, twin_url: str, bootstrap_token: Optional[str] = None) -> None:
    existing = _ROUTES.get(domain)
    if existing is None:
        if not bootstrap_token:
            raise ValueError(f"register({domain}): bootstrap_token required for new domain")
        _ROUTES[domain] = Route(domain=domain, twin_url=twin_url, bootstrap_token=bootstrap_token)
        return
    _ROUTES[domain] = replace(
        existing,
        twin_url=twin_url,
        bootstrap_token=bootstrap_token or existing.bootstrap_token,
    )


def lookup(host: str) -> Optional[Route]:
    return _ROUTES.get(host)


def all_domains() -> list[str]:
    return list(_ROUTES.keys())
